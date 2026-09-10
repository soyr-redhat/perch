import {test} from 'node:test';
import assert from 'node:assert/strict';
import {JSDOM} from '../frontend/node_modules/jsdom/lib/api.js';
const dom = new JSDOM('<!doctype html><body></body>', {pretendToBeVisual: true});
for (const name of ['window','document','navigator','MutationObserver','Window','HTMLElement','Node'])
  Object.defineProperty(globalThis, name, {value: dom.window[name], configurable: true});
globalThis.requestAnimationFrame = dom.window.requestAnimationFrame.bind(dom.window);
globalThis.cancelAnimationFrame = dom.window.cancelAnimationFrame.bind(dom.window);
dom.window.Range.prototype.getClientRects = () => [];
dom.window.Range.prototype.getBoundingClientRect = () => ({left:0,right:0,top:0,bottom:0,width:0,height:0});
const {createEditor, tabIndent, fileLanguage} = await import('../frontend/editor.js');
const {formatDocument} = await import('../frontend/formatter.js');
const {EditorView} = await import('../frontend/node_modules/@codemirror/view/dist/index.js');
const {undo, indentLess} = await import('../frontend/node_modules/@codemirror/commands/dist/index.js');
const {syntaxTree} = await import('../frontend/node_modules/@codemirror/language/dist/index.js');
function editor(t, filename, value, options = {}) {
  const element = document.body.appendChild(document.createElement('div'));
  const edits=[];
  const code = createEditor({element, filename, value, onChange: (name,text) => edits.push({name,text}), formatter: async()=>({formatDocument}), ...options});
  const view = EditorView.findFromDOM(element.querySelector('.cm-editor'));
  t.after(()=>{code.destroy();element.remove();});
  return {code, view, element, edits};
}
test('Tab inserts indentation at the cursor; selections indent and outdent without newlines', t => {
  const {code,view}=editor(t,'config.yaml','one: true\ntwo: false');
  view.dispatch({selection:{anchor:2}});tabIndent(view);
  assert.equal(code.value,'on  e: true\ntwo: false');
  undo(view);
  view.dispatch({selection:{anchor:0,head:view.state.doc.length}});tabIndent(view);
  assert.equal(code.value,'  one: true\n  two: false');
  indentLess(view);assert.equal(code.value,'one: true\ntwo: false');
  code.setReadOnly(true);assert.equal(tabIndent(view),false);assert.equal(code.value,'one: true\ntwo: false');
});
test('keyboard Tab and Enter are distinct, wrapping is enabled, and Markdown parses frontmatter', t => {
  const {code,view}=editor(t,'SKILL.md','---\nname: review\n---\n# Review\n');
  assert.equal(view.lineWrapping,true);
  assert.match(syntaxTree(view.state).toString(),/Frontmatter/);
  view.dispatch({selection:{anchor:view.state.doc.length}});code.focus();
  view.contentDOM.dispatchEvent(new window.KeyboardEvent('keydown',{key:'Tab',code:'Tab',keyCode:9,bubbles:true,cancelable:true}));
  assert.ok(code.value.endsWith('\n  '));
  view.contentDOM.dispatchEvent(new window.KeyboardEvent('keydown',{key:'Enter',code:'Enter',bubbles:true,cancelable:true}));
  assert.equal(code.value.split('\n').length,6);
  view.contentDOM.dispatchEvent(new window.KeyboardEvent('keydown',{key:'Escape',keyCode:27,bubbles:true,cancelable:true}));
  const tab=new window.KeyboardEvent('keydown',{key:'Tab',keyCode:9,bubbles:true,cancelable:true});
  view.contentDOM.dispatchEvent(tab);assert.equal(tab.defaultPrevented,false);
});
test('file switching retains individual drafts, selections, and undo history', t => {
  const {code,view,edits}=editor(t,'SKILL.md','# Original');
  view.dispatch({changes:{from:10,insert:' text'},selection:{anchor:15}});
  code.open('config.yaml','enabled: true');
  view.dispatch({changes:{from:0,insert:'# Comment\n'}});
  code.open('SKILL.md','# Original');
  assert.equal(code.value,'# Original text');assert.equal(view.state.selection.main.head,15);
  undo(view);assert.equal(code.value,'# Original');
  code.open('config.yaml','enabled: true');assert.equal(code.value,'# Comment\nenabled: true');
  assert.equal(edits.at(-1).name,'SKILL.md');
});
test('formatting JSON is undoable and invalid YAML preserves the draft', async t => {
  const {code,view,element}=editor(t,'server.json','{"nested":{"on":true}}');
  await code.format();assert.match(code.value,/\n  "nested"/);
  undo(view);assert.equal(code.value,'{"nested":{"on":true}}');
  code.open('broken.yaml','items: [unterminated');await code.format();
  assert.equal(code.value,'items: [unterminated');assert.ok(element.querySelector('.code-status').textContent.length>0);
});
test('formatting preserves Markdown hard breaks and YAML comments, uses spaces, and detects file types',async()=>{
  const md=await formatDocument('---\nname:   review\n---\n# title\n\nline one  \nline two\n','SKILL.md');
  assert.match(md.formatted,/line one  \nline two/);assert.match(md.formatted,/name: review/);
  const yaml=await formatDocument('# Keep this\na:    1\nb: [true,false]\n','config.yml');
  assert.match(yaml.formatted,/# Keep this/);assert.match(yaml.formatted,/a: 1/);assert.ok(!yaml.formatted.includes('\t'));
  assert.equal(fileLanguage('SKILL.MD'),'markdown');assert.equal(fileLanguage('script.py'),'text');
});
test('a delayed formatter cannot replace newer typing or another file',async t=>{
  let resolve;
  const {code,view}=editor(t,'server.json','{}',{formatter:()=>new Promise(done=>{resolve=done;})});
  const pending=code.format();view.dispatch({changes:{from:1,insert:'"new":true'}});
  resolve({formatDocument});await pending;assert.equal(code.value,'{"new":true}');
  const other=code.format();code.open('settings.yaml','new: true');
  resolve({formatDocument});await other;assert.equal(code.value,'new: true');
});
