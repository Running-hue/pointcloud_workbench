'use strict';
// Execute the HTML returned by the actual HTTP handler, including its final draw().
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const page = fs.readFileSync(0, 'utf8');
const elements = new Map();
let draws = 0;
const context2d = new Proxy({}, {get:(_, key) => key === 'fillRect' ? () => { draws++; } : () => {}});
function element() {
  return {hidden:false, disabled:false, value:'rgb', children:[], clientWidth:900, clientHeight:650,
    classList:{toggle() {}}, append(child) { this.children.push(child); },
    getContext:() => context2d, showModal() {}, close() {}};
}
for (const match of page.matchAll(/id="([^"]+)"/g)) elements.set(match[1], element());
const irOption = element();
const document = {
  getElementById:id => elements.get(id), createElement:element,
  querySelector(selector) {
    if (selector === '#mode option[value="ir"]') return irOption;
    throw new Error('Unexpected or corrupted selector: ' + selector);
  },
  querySelectorAll:() => []
};
const script = page.match(/<script>([\s\S]*?)<\/script>/)[1];
vm.runInNewContext(script, {document, window:{}, devicePixelRatio:1}, {timeout:5000});
assert.ok(draws > 0, 'viewer must draw points rather than stop during initialization');
assert.match(elements.get('count').textContent, /点$/);
const mode = page.includes('"input_mode":"rgb"') ? 'rgb' : 'ir';
assert.equal(elements.get('mode').value, mode);
assert.equal(irOption.disabled, mode === 'rgb');
const filename = mode === 'rgb' ? 'cloud_rgb_mm.ply' : 'cloud_ir_mm.ply';
const other = mode === 'rgb' ? 'cloud_ir_mm.ply' : 'cloud_rgb_mm.ply';
assert.ok(page.includes('file='+filename) || page.includes('href="'+filename+'"'));
assert.ok(!page.includes('file='+other) && !page.includes('href="'+other+'"'));
process.stdout.write(JSON.stringify({draws, mode, count:elements.get('count').textContent}));
