// Resolve obfuscator.io string-array indirection in vm.beau.js -> resolved.js (readable).
//
// decode(n) = arr[(n + C) % L]  (L=2119, C=2085 for THIS build).
// The decoder is aliased per-function via PARAMETER reassignment, e.g.
//     N.runProgram = function (d, W4, kq, k) { return W4 = {d:1368}, kq = ds, ... kq(W4.d) ... }
// so `kq` (alias of ds) and `W4` (numeric map) are bound by the param list and reassigned in
// the body's leading sequence-expression. We therefore collect aliases + numeric maps SCOPE-
// AWARE (by Babel binding identity), covering both `var X = ...` and `X = ...` (assignment /
// param-reassign), then replace every  alias(NUM)  and  alias(MAP.key)  with the string literal.
const fs = require("fs");
const parser = require("@babel/parser");
const traverse = require("@babel/traverse").default;
const t = require("@babel/types");
const gen = require("@babel/generator").default;

const src = fs.readFileSync("vm.beau.js", "utf8");
const m = src.match(/return Pp = (.+?)\.split\(.;.\)/s);
if (!m) throw new Error("string array (return Pp = '...'.split(';')) not found");
const arr = eval(m[1]).split(";");
const L = arr.length, C = 2085;
const decode = (n) => arr[(((n + C) % L) + L) % L];

const ast = parser.parse(src);

// A numeric-literal map object?  { d: 1368, k: 944, ... }  (every value an integer literal)
function numMap(node) {
  if (!t.isObjectExpression(node) || node.properties.length === 0) return null;
  const o = {};
  for (const pr of node.properties) {
    if (!t.isObjectProperty(pr) || pr.computed) return null;
    const k = t.isIdentifier(pr.key) ? pr.key.name
      : (t.isStringLiteral(pr.key) || t.isNumericLiteral(pr.key)) ? String(pr.key.value)
      : null;
    if (k == null) return null;
    if (!t.isNumericLiteral(pr.value)) return null;
    o[k] = pr.value.value;
  }
  return o;
}

const SEED = new Set(["ds"]);                 // module-level base decoder name (ds = f)
const aliasBindings = new Set();              // Set<Binding> : identifiers that ARE the decoder
const mapBindings = new Map();                // Map<Binding, {key:idx}>

function rhsIsAlias(scope, idNode) {
  if (!t.isIdentifier(idNode)) return false;
  if (SEED.has(idNode.name)) return true;
  const b = scope.getBinding(idNode.name);
  return !!(b && aliasBindings.has(b));
}

// --- Collection: fixpoint over alias chains (kq=ds ; xx=kq ; ...) + all numeric maps. ---
let changed = true, iter = 0;
while (changed && iter++ < 12) {
  changed = false;
  traverse(ast, {
    "VariableDeclarator|AssignmentExpression"(p) {
      let id, init;
      if (p.isVariableDeclarator()) { id = p.node.id; init = p.node.init; }
      else { if (p.node.operator !== "=") return; id = p.node.left; init = p.node.right; }
      if (!t.isIdentifier(id) || !init) return;
      const b = p.scope.getBinding(id.name);
      if (!b) return;                          // unbound implicit global (e.g. ds itself) -> SEED covers it
      const mm = numMap(init);
      if (mm) { if (!mapBindings.has(b)) { mapBindings.set(b, mm); changed = true; } return; }
      if (rhsIsAlias(p.scope, init) && !aliasBindings.has(b)) { aliasBindings.add(b); changed = true; }
    },
  });
}

// --- Replacement: alias(NUM) and alias(MAP.key) / alias(MAP["key"]) -> string literal. ---
function isDecoderCallee(scope, idNode) {
  if (!t.isIdentifier(idNode)) return false;
  if (SEED.has(idNode.name)) return true;
  const b = scope.getBinding(idNode.name);
  return !!(b && aliasBindings.has(b));
}
function mapIndex(scope, argNode) {
  if (t.isNumericLiteral(argNode)) return argNode.value;
  if (!t.isMemberExpression(argNode) || !t.isIdentifier(argNode.object)) return null;
  const b = scope.getBinding(argNode.object.name);
  const mm = b && mapBindings.get(b);
  if (!mm) return null;
  let key = null;
  if (!argNode.computed && t.isIdentifier(argNode.property)) key = argNode.property.name;
  else if (argNode.computed && t.isStringLiteral(argNode.property)) key = argNode.property.value;
  if (key == null || !(key in mm)) return null;
  return mm[key];
}

let n = 0;
traverse(ast, {
  CallExpression(p) {
    const c = p.node.callee, a = p.node.arguments;
    if (a.length !== 1 || !isDecoderCallee(p.scope, c)) return;
    const idx = mapIndex(p.scope, a[0]);
    if (idx == null) return;
    const s = decode(idx);
    if (typeof s === "string") { p.replaceWith(t.stringLiteral(s)); n++; }
  },
});

fs.writeFileSync("resolved.js", gen(ast, { comments: false, compact: false, jsescOption: { minimal: true } }).code);
console.log(`resolved ${n} decoder calls | aliases=${aliasBindings.size} maps=${mapBindings.size} | arr.len=${L} C=${C}`);
