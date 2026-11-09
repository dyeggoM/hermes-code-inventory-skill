#!/usr/bin/env node
'use strict';

const fs = require('fs');
const path = require('path');
const babel = require('@babel/parser');

const EXTS = new Set(['.js', '.jsx', '.mjs', '.cjs', '.ts', '.tsx', '.vue']);
const SKIP_DIRS = new Set(['node_modules', '.git', '.code-inventory', 'dist', 'build', 'coverage', '.next', 'out', 'vendor']);
const CALLABLE = new Set(['function', 'component', 'method', 'constructor', 'local_function']);
const REF_NAMES = new Set(['ref', 'reactive', 'computed', 'shallowRef', 'shallowReactive', 'toRef']);
const PARSER_OPTIONS = {
  sourceType: 'module',
  plugins: ['jsx', 'typescript', 'decorators', 'classProperties', 'exportDefaultFrom'],
  allowImportExportEverywhere: true,
};

function walk(dir, out) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    if (entry.isDirectory()) {
      if (!SKIP_DIRS.has(entry.name)) walk(path.join(dir, entry.name), out);
    } else if (entry.isFile() && EXTS.has(path.extname(entry.name).toLowerCase())) {
      out.push(path.join(dir, entry.name));
    }
  }
}

function relPosix(root, abs) {
  return path.relative(root, abs).split(path.sep).join('/');
}

function moduleFromRel(rel) {
  return rel.replace(/\.(js|jsx|mjs|cjs|ts|tsx|vue)$/i, '');
}

function qualname(key) {
  const first = key.indexOf(':');
  return key.slice(key.indexOf(':', first + 1) + 1);
}

function childKey(ctx, parentKey, name) {
  if (!parentKey || parentKey === ctx.moduleKey) return `js:${ctx.module}:${name}`;
  return `js:${ctx.module}:${qualname(parentKey)}.${name}`;
}

function decl(ctx, kind, name, key, parentKey, signature, line) {
  ctx.declarations.push({
    kind, name, symbol_key: key, parent_symbol_key: parentKey,
    signature: signature || '', line: line || 1,
  });
}

function paramsText(node) {
  const params = node.params || [];
  return params.map((p) => {
    if (p.type === 'Identifier') return p.name;
    if (p.type === 'RestElement' && p.argument.type === 'Identifier') return '...' + p.argument.name;
    if (p.type === 'AssignmentPattern' && p.left.type === 'Identifier') return p.left.name + ' = ...';
    return 'param';
  }).join(', ');
}

function slice(code, start, end, limit = 90) {
  return code.slice(start, end).replace(/\s+/g, ' ').slice(0, limit);
}

function returnsJSX(fn) {
  if (!fn.body) return false;
  if (fn.body.type === 'JSXElement' || fn.body.type === 'JSXFragment') return true;
  if (fn.body.type === 'BlockStatement') {
    for (const stmt of fn.body.body) {
      if (stmt.type === 'ReturnStatement' && stmt.argument) {
        if (stmt.argument.type === 'JSXElement' || stmt.argument.type === 'JSXFragment') return true;
      }
    }
  }
  return false;
}

function isComponent(name, fn) {
  return /^[A-Z]/.test(name) && returnsJSX(fn);
}

function decoratorName(dec) {
  const expr = dec.expression;
  if (expr.type === 'Identifier') return expr.name;
  if (expr.type === 'CallExpression' && expr.callee.type === 'Identifier') return expr.callee.name;
  if (expr.type === 'MemberExpression' && expr.property.type === 'Identifier') return expr.property.name;
  return 'decorator';
}

function vueScript(text) {
  const match = /<script\b([^>]*)>([\s\S]*?)<\/script>/i.exec(text);
  if (!match) return { code: null, offset: 0, setup: false };
  const before = text.slice(0, match.index + match[0].indexOf('>') + 1);
  return {
    code: match[2],
    offset: before.split('\n').length - 1,
    setup: /\bsetup\b/.test(match[1]),
  };
}

function resolveImportModule(root, fileAbs, source, moduleNames) {
  if (source.startsWith('.')) {
    const targetAbs = path.resolve(path.dirname(fileAbs), source);
    const rel = path.relative(root, targetAbs);
    if (rel.startsWith('..') || path.isAbsolute(rel)) return null;
    const module = moduleFromRel(rel.split(path.sep).join('/'));
    return moduleNames.has(module) ? module : null;
  }
  // Bundler/tsconfig path aliases (`@services/items`, `@/components/X`, `src/...`) are not resolvable
  // from the import string alone, so match the alias tail against the project's module names.
  const candidate = source.replace(/^[~@/]+/, '');
  if (!candidate) return null;
  const hits = [];
  for (const mod of moduleNames) {
    if (mod === candidate || mod.endsWith('/' + candidate)) hits.push(mod);
  }
  if (hits.length === 0) return null;
  hits.sort((a, b) => a.length - b.length);
  return hits[0];
}

function recordImport(node, ctx, root, fileAbs, moduleNames) {
  if (!node.source || node.source.type !== 'StringLiteral') return;
  const targetModule = resolveImportModule(root, fileAbs, node.source.value, moduleNames);
  if (!targetModule) return;
  for (const spec of node.specifiers || []) {
    if (spec.type === 'ImportSpecifier') {
      ctx.imports.set(spec.local.name, { type: 'named', module: targetModule, imported: spec.imported.name });
    } else if (spec.type === 'ImportDefaultSpecifier') {
      ctx.imports.set(spec.local.name, { type: 'default', module: targetModule });
    } else if (spec.type === 'ImportNamespaceSpecifier') {
      ctx.imports.set(spec.local.name, { type: 'namespace', module: targetModule });
    }
  }
}

function visitChildren(node, parentKey, scope, classKey, ctx) {
  for (const key of Object.keys(node)) {
    if (['type', 'loc', 'start', 'end', 'extra', 'tokens', 'comments', 'range'].includes(key)) continue;
    const value = node[key];
    if (Array.isArray(value)) {
      for (const child of value) if (child && typeof child.type === 'string') visit(child, parentKey, scope, classKey, ctx);
    } else if (value && typeof value.type === 'string') {
      visit(value, parentKey, scope, classKey, ctx);
    }
  }
}

function recordCall(node, scope, classKey, ctx) {
  const callee = node.callee;
  if (!callee) return;
  let name = null;
  let kind = null;
  let receiver = null;
  if (callee.type === 'Identifier') {
    name = callee.name;
    kind = 'ident';
  } else if (callee.type === 'MemberExpression' || callee.type === 'OptionalMemberExpression') {
    if (!callee.computed && callee.property.type === 'Identifier') name = callee.property.name;
    if (callee.computed && callee.property.type === 'StringLiteral') name = callee.property.value;
    if (name) {
      kind = 'member';
      if (callee.object.type === 'Identifier') receiver = callee.object.name;
      else if (callee.object.type === 'ThisExpression') receiver = 'this';
    }
  }
  if (!name) return;
  ctx.rawCalls.push({
    caller: scope, name, kind, receiver, classKey,
    line: node.loc.start.line + ctx.lineOffset,
  });
}

function objectMemberName(member) {
  if (member.computed) return null;
  if (member.key.type === 'Identifier') return member.key.name;
  if (member.key.type === 'StringLiteral' || member.key.type === 'NumericLiteral') return String(member.key.value);
  return null;
}

// Object literals are the common TS "service object" shape: `export const svc = { getAll() {} }`.
// Without this, the whole service layer indexes as interfaces with no callables.
function visitObjectBody(objNode, objKey, ctx) {
  for (const member of objNode.properties || []) {
    if (member.type === 'SpreadElement') { visit(member.argument, objKey, objKey, null, ctx); continue; }
    const name = objectMemberName(member);
    if (!name) { visitChildren(member, objKey, objKey, null, ctx); continue; }
    if (member.type === 'ObjectMethod') {
      const key = childKey(ctx, objKey, name);
      const kind = name === 'constructor' ? 'constructor' : 'method';
      decl(ctx, kind, name, key, objKey, `${name}(${paramsText(member)})`, member.loc.start.line + ctx.lineOffset);
      visit(member.body, key, key, objKey, ctx);
      continue;
    }
    if (member.type === 'ObjectProperty') {
      const value = member.value;
      if (value && (value.type === 'ArrowFunctionExpression' || value.type === 'FunctionExpression' ||
                    value.type === 'ObjectMethod')) {
        const key = childKey(ctx, objKey, name);
        decl(ctx, 'method', name, key, objKey, `${name}(${paramsText(value)})`, member.loc.start.line + ctx.lineOffset);
        ctx.locals.set(name, key);
        visit(value, key, key, objKey, ctx);
      } else if (value) {
        visit(value, objKey, objKey, null, ctx);
      }
      continue;
    }
    visitChildren(member, objKey, objKey, null, ctx);
  }
}

function visitClassBody(body, classKey, ctx) {
  for (const member of body.body || []) {
    if (member.type === 'ClassMethod' || member.type === 'MethodDefinition') {
      const keyName = member.computed ? null : (member.key.name || member.key.value);
      if (!keyName) continue;
      const name = keyName === 'constructor' ? 'constructor' : keyName;
      const kind = keyName === 'constructor' ? 'constructor' : 'method';
      const key = childKey(ctx, classKey, name);
      decl(ctx, kind, name, key, classKey, `${name}(${paramsText(member)})`, member.loc.start.line + ctx.lineOffset);
      visit(member.value || member.body, key, key, classKey, ctx);
    } else if (member.type === 'ClassProperty' || member.type === 'PropertyDefinition') {
      const name = member.computed ? null : (member.key.name || member.key.value);
      if (!name) continue;
      const key = childKey(ctx, classKey, name);
      const fieldText = member.value
        ? `${name} = ${slice(ctx.code, member.value.start, member.value.end)}`
        : name;
      decl(ctx, 'field', name, key, classKey, fieldText,
           member.loc.start.line + ctx.lineOffset);
      if (member.value) visit(member.value, classKey, classKey, classKey, ctx);
    }
  }
}

function visit(node, parentKey, scope, classKey, ctx) {
  if (!node || typeof node.type !== 'string') return;

  if (node.type === 'Program') {
    for (const stmt of node.body) visit(stmt, parentKey, scope, classKey, ctx);
    return;
  }

  if (node.type === 'ImportDeclaration') {
    recordImport(node, ctx, ctx.root, ctx.fileAbs, ctx.moduleNames);
    return;
  }

  if (node.type === 'ExportNamedDeclaration') {
    if (node.declaration) {
      visit(node.declaration, parentKey, scope, classKey, ctx);
      if (node.declaration.id && node.declaration.id.name) {
        ctx.exports.set(node.declaration.id.name, ctx.locals.get(node.declaration.id.name) || null);
      }
      if (node.declaration.type === 'VariableDeclaration') {
        for (const d of node.declaration.declarations) {
          if (d.id.type === 'Identifier') ctx.exports.set(d.id.name, ctx.locals.get(d.id.name) || null);
        }
      }
    }
    for (const spec of node.specifiers || []) {
      if (spec.type === 'ExportSpecifier') {
        ctx.exports.set(spec.exported.name, ctx.locals.get(spec.local.name) || null);
        ctx.pendingExports.push([spec.exported.name, spec.local.name]);
      }
    }
    return;
  }

  if (node.type === 'ExportDefaultDeclaration') {
    const target = node.declaration;
    if (target.type === 'FunctionDeclaration' || target.type === 'ClassDeclaration') {
      visit(target, parentKey, scope, classKey, ctx);
      if (target.id && target.id.name) ctx.defaultLocalName = target.id.name;
    } else if (target.type === 'Identifier') {
      ctx.defaultLocalName = target.name;
    } else if (target.type === 'FunctionExpression' || target.type === 'ArrowFunctionExpression') {
      if (returnsJSX(target)) {
        const key = childKey(ctx, parentKey, 'default');
        decl(ctx, 'component', 'default', key, parentKey, 'default component', target.loc.start.line + ctx.lineOffset);
        ctx.defaultKey = key;
        visit(target, key, key, null, ctx);
      } else {
        visit(target, parentKey, scope, classKey, ctx);
      }
    } else {
      visit(target, parentKey, scope, classKey, ctx);
    }
    return;
  }

  if (node.type === 'FunctionDeclaration') {
    const name = node.id && node.id.name;
    if (!name) { visitChildren(node, parentKey, scope, classKey, ctx); return; }
    const key = childKey(ctx, parentKey, name);
    const kind = isComponent(name, node) ? 'component' : 'function';
    decl(ctx, kind, name, key, parentKey, `function ${name}(${paramsText(node)})`, node.loc.start.line + ctx.lineOffset);
    ctx.locals.set(name, key);
    for (const p of node.params || []) visit(p, key, key, null, ctx);
    visit(node.body, key, key, null, ctx);
    return;
  }

  if (node.type === 'VariableDeclaration') {
    for (const d of node.declarations) {
      if (d.id.type !== 'Identifier') {
        if (d.init) visit(d.init, parentKey, scope, classKey, ctx);
        continue;
      }
      const name = d.id.name;
      let init = d.init;
      if (init && (init.type === 'ArrowFunctionExpression' || init.type === 'FunctionExpression')) {
        const key = childKey(ctx, parentKey, name);
        const kind = isComponent(name, init) ? 'component' : (parentKey ? 'local_function' : 'function');
        decl(ctx, kind, name, key, parentKey,
             `${kind} ${name}(${paramsText(init)})`, d.loc.start.line + ctx.lineOffset);
        ctx.locals.set(name, key);
        visit(init, key, key, null, ctx);
      } else if (init && init.type === 'ObjectExpression') {
        const key = childKey(ctx, parentKey, name);
        decl(ctx, 'object', name, key, parentKey,
             `const ${name} = { ${(init.properties || []).length} members }`, d.loc.start.line + ctx.lineOffset);
        ctx.locals.set(name, key);
        visitObjectBody(init, key, ctx);
      } else if (init && ctx.vueSetup && parentKey === ctx.componentKey &&
                 init.type === 'CallExpression' && init.callee.type === 'Identifier' &&
                 REF_NAMES.has(init.callee.name)) {
        const key = childKey(ctx, parentKey, name);
        decl(ctx, 'property', name, key, parentKey,
             `const ${name} = ${slice(ctx.code, init.start, init.end)}`, d.loc.start.line + ctx.lineOffset);
        ctx.locals.set(name, key);
        visit(init, parentKey, scope, classKey, ctx);
      } else if (init) {
        visit(init, parentKey, scope, classKey, ctx);
      }
    }
    return;
  }

  if (node.type === 'ClassDeclaration') {
    const name = node.id && node.id.name;
    if (!name) { visitChildren(node, parentKey, scope, classKey, ctx); return; }
    const key = childKey(ctx, parentKey, name);
    decl(ctx, 'class', name, key, parentKey, `class ${name}`, node.loc.start.line + ctx.lineOffset);
    ctx.locals.set(name, key);
    for (const dec of node.decorators || []) {
      const dn = decoratorName(dec);
      const decKey = childKey(ctx, key, dn);
      decl(ctx, 'decorator', dn, decKey, key, slice(ctx.code, dec.start, dec.end), dec.loc.start.line + ctx.lineOffset);
    }
    visitClassBody(node.body, key, ctx);
    return;
  }

  if (node.type === 'TSInterfaceDeclaration' || node.type === 'InterfaceDeclaration') {
    const name = node.id && node.id.name;
    if (name) {
      const key = childKey(ctx, parentKey, name);
      decl(ctx, 'interface', name, key, parentKey, `interface ${name}`, node.loc.start.line + ctx.lineOffset);
      ctx.locals.set(name, key);
    }
    return;
  }

  if (node.type === 'ObjectExpression') {
    visitObjectBody(node, parentKey, ctx);
    return;
  }

  if (node.type === 'CallExpression' || node.type === 'OptionalCallExpression') {
    recordCall(node, scope, classKey, ctx);
  }

  if (node.type === 'FunctionExpression' || node.type === 'ArrowFunctionExpression') {
    for (const p of node.params || []) visit(p, parentKey, scope, classKey, ctx);
    visit(node.body, parentKey, scope, classKey, ctx);
    return;
  }

  visitChildren(node, parentKey, scope, classKey, ctx);
}

function parseFile(ctx, code, isVueScript) {
  try {
    const ast = babel.parse(code, PARSER_OPTIONS);
    return ast;
  } catch (err) {
    ctx.diagnostics.push({
      severity: 'error',
      message: `parse error${isVueScript ? ' in <script>' : ''}: ${err.message}`,
    });
    return null;
  }
}

function analyzeFile(root, fileAbs, moduleNames) {
  const rel = relPosix(root, fileAbs);
  const module = moduleFromRel(rel);
  const moduleKey = `js:${module}:<module>`;
  const ctx = {
    rel, module, moduleKey, fileAbs, root, moduleNames,
    imports: new Map(), locals: new Map(), exports: new Map(),
    declarations: [], rawCalls: [], diagnostics: [],
    pendingExports: [], defaultLocalName: null, defaultKey: null,
    componentKey: null, vueSetup: false, lineOffset: 0, code: '',
  };
  decl(ctx, 'module', module, moduleKey, null, `module ${module}`, 1);

  let text = fs.readFileSync(fileAbs, 'utf8');
  const ext = path.extname(fileAbs).toLowerCase();
  ctx.code = text;

  if (ext === '.vue') {
    const stem = path.posix.basename(module);
    ctx.componentKey = `js:${module}:${stem}`;
    decl(ctx, 'component', stem, ctx.componentKey, null, `SFC ${stem}`, 1);
    const script = vueScript(text);
    if (script.code && script.code.trim()) {
      ctx.code = script.code;
      ctx.lineOffset = script.offset;
      ctx.vueSetup = script.setup;
      const ast = parseFile(ctx, script.code, true);
      if (ast) visit(ast, ctx.componentKey, moduleKey, null, ctx);
    }
  } else {
    const ast = parseFile(ctx, text, false);
    if (ast) visit(ast, null, moduleKey, null, ctx);
  }

  if (ctx.defaultLocalName) ctx.defaultKey = ctx.locals.get(ctx.defaultLocalName) || null;
  for (const [exported, local] of ctx.pendingExports) {
    const key = ctx.locals.get(local);
    if (key) ctx.exports.set(exported, key);
  }
  for (const [name, key] of ctx.exports) if (!key) ctx.exports.delete(name);

  return ctx;
}

function resolveCall(ctx, raw, symbolIndex, moduleExports, moduleDefault) {
  let calleeKey = null;
  if (raw.kind === 'ident') {
    const imp = ctx.imports.get(raw.name);
    if (imp) {
      if (imp.type === 'named') {
        const exports = moduleExports.get(imp.module);
        if (exports) calleeKey = exports.get(imp.imported) || null;
      } else if (imp.type === 'default') {
        calleeKey = moduleDefault.get(imp.module) || null;
      }
    } else if (ctx.locals.has(raw.name)) {
      calleeKey = ctx.locals.get(raw.name);
    }
  } else if (raw.kind === 'member') {
    if (raw.receiver === 'this' && raw.classKey) {
      calleeKey = raw.classKey + '.' + raw.name;
    } else if (raw.receiver && ctx.imports.has(raw.receiver)) {
      const imp = ctx.imports.get(raw.receiver);
      if (imp.type === 'namespace') {
        const exports = moduleExports.get(imp.module);
        if (exports) calleeKey = exports.get(raw.name) || null;
      } else {
        // `import { itemsService } from './items'` then `itemsService.getAll()` — the export is an
        // object literal, so the callable is the child key, not the object itself.
        const exported = imp.type === 'named'
          ? (moduleExports.get(imp.module) || new Map()).get(imp.imported)
          : moduleDefault.get(imp.module);
        if (exported && symbolIndex.has(exported + '.' + raw.name)) calleeKey = exported + '.' + raw.name;
      }
    } else if (raw.receiver && ctx.locals.has(raw.receiver)) {
      const base = ctx.locals.get(raw.receiver);
      if (base && symbolIndex.has(base + '.' + raw.name)) calleeKey = base + '.' + raw.name;
    }
  }
  if (calleeKey && symbolIndex.has(calleeKey) && CALLABLE.has(symbolIndex.get(calleeKey).kind)) {
    return {
      caller_symbol_key: raw.caller,
      callee_symbol_key: calleeKey,
      callee_name: raw.name,
      line: raw.line,
      external: 0,
    };
  }
  return {
    caller_symbol_key: raw.caller,
    callee_symbol_key: null,
    callee_name: raw.name,
    line: raw.line,
    external: 1,
  };
}

function analyzeProject(root) {
  root = path.resolve(root);
  const files = [];
  walk(root, files);
  files.sort();
  const moduleNames = new Set(files.map((f) => moduleFromRel(relPosix(root, f))));
  const contexts = files.map((f) => analyzeFile(root, f, moduleNames));

  const symbolIndex = new Map();
  const moduleExports = new Map();
  const moduleDefault = new Map();
  for (const ctx of contexts) {
    for (const d of ctx.declarations) symbolIndex.set(d.symbol_key, d);
    moduleExports.set(ctx.module, ctx.exports);
    moduleDefault.set(ctx.module, ctx.defaultKey || null);
  }

  return {
    files: contexts.map((ctx) => ({
      path: ctx.rel,
      declarations: ctx.declarations,
      calls: ctx.rawCalls.map((raw) => resolveCall(ctx, raw, symbolIndex, moduleExports, moduleDefault)),
      diagnostics: ctx.diagnostics,
    })),
  };
}

function main() {
  const args = process.argv.slice(2);
  if (args[0] !== 'analyze-project') {
    process.stderr.write('usage: node analyze.js analyze-project --root <absolute dir>\n');
    process.exit(2);
  }
  let root = null;
  for (let i = 1; i < args.length; i += 1) {
    if (args[i] === '--root' && i + 1 < args.length) root = args[i + 1];
  }
  if (!root) {
    process.stderr.write('usage: node analyze.js analyze-project --root <absolute dir>\n');
    process.exit(2);
  }
  process.stdout.write(JSON.stringify(analyzeProject(root)) + '\n');
}

main();
