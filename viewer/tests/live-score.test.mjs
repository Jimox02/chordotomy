import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
const context = vm.createContext({window:{}});
vm.runInContext(readFileSync(new URL('../../src/chordotomy/score.js',import.meta.url),'utf8'),context);
const ui=context.window.ScoreUI;
test('score chart preserves major seventh and added-tone labels',()=>{
 assert.equal(ui.name('C:maj7'),'Cmaj7');
 assert.equal(ui.name('C:7'),'C7');
 assert.equal(ui.name('A:min7'),'Am7');
 assert.equal(ui.name('F:maj(9)'),'Fadd9');
 assert.equal(ui.name(null),'');
 assert.equal(ui.name('N'),'N.C.');
});
test('unknown function stays blank and complex functions have Chinese labels',()=>{
 assert.equal(ui.describe({}), '');
 assert.equal(ui.describe({numeral:'V7/V',role:'secondary_dominant',target:'V'}),'副属和弦 → V');
 assert.equal(ui.describe({numeral:'vii°/ii',role:'secondary_dominant',target:'ii'}),'副导和弦 → ii');
 assert.equal(ui.describe({numeral:'iv',role:'borrowed'}),'同主音调式借用');
});
