import {EditorState, EditorSelection, Compartment, Transaction} from '@codemirror/state';
import {EditorView, keymap, lineNumbers, drawSelection, highlightActiveLine} from '@codemirror/view';
import {defaultKeymap, history, historyKeymap, indentMore, indentLess, isolateHistory, temporarilySetTabFocusMode} from '@codemirror/commands';
import {indentUnit, syntaxHighlighting, HighlightStyle, bracketMatching} from '@codemirror/language';
import {markdown} from '@codemirror/lang-markdown';
import {yaml, yamlFrontmatter, yamlLanguage} from '@codemirror/lang-yaml';
import {json, jsonLanguage} from '@codemirror/lang-json';
import {tags} from '@lezer/highlight';

export function fileLanguage(filename) {
  if (/\.(md|markdown)$/i.test(filename)) return 'markdown';
  if (/\.ya?ml$/i.test(filename)) return 'yaml';
  if (/\.json$/i.test(filename)) return 'json';
  return 'text';
}
function languageSupport(filename) {
  const language = fileLanguage(filename);
  if (language === 'markdown') return yamlFrontmatter({content: markdown({addKeymap: false, codeLanguages: name => {
    if (/^ya?ml$/i.test(name)) return yamlLanguage;
    if (/^json$/i.test(name)) return jsonLanguage;
    return null;
  }})});
  return language === 'yaml' ? yaml() : language === 'json' ? json() : [];
}
export function tabIndent(view) {
  if (view.state.readOnly) return false;
  if (view.state.selection.ranges.some(range => !range.empty)) return indentMore(view);
  view.dispatch(view.state.changeByRange(range => {
    const column = range.head - view.state.doc.lineAt(range.head).from;
    const text = ' '.repeat(2 - column % 2);
    return {changes: {from: range.head, insert: text}, range: EditorSelection.cursor(range.head + text.length)};
  }), {userEvent: 'input.indent', scrollIntoView: true});
  return true;
}
const highlighting = HighlightStyle.define([
  {tag: tags.heading, class: 'syntax-heading'},
  {tag: tags.strong, class: 'syntax-strong'},
  {tag: tags.emphasis, class: 'syntax-emphasis'},
  {tag: [tags.string, tags.inserted], class: 'syntax-string'},
  {tag: [tags.propertyName, tags.attributeName], class: 'syntax-property'},
  {tag: [tags.keyword, tags.bool, tags.null, tags.number], class: 'syntax-value'},
  {tag: [tags.comment, tags.meta, tags.processingInstruction], class: 'syntax-comment'},
  {tag: [tags.link, tags.url], class: 'syntax-link'},
  {tag: tags.monospace, class: 'syntax-code'},
]);
let formatterWorker, requestId = 0;
const pendingFormats = new Map();
function stopFormatter(message) {
  formatterWorker?.terminate(); formatterWorker = null;
  for (const pending of pendingFormats.values()) {clearTimeout(pending.timer); pending.reject(new Error(message));}
  pendingFormats.clear();
}
export async function loadFormatter() {
  if (!formatterWorker) {
    formatterWorker = new Worker('/formatter.min.js');
    formatterWorker.onmessage = ({data}) => {
      const pending = pendingFormats.get(data.id);
      if (!pending) return;
      clearTimeout(pending.timer); pendingFormats.delete(data.id);
      if (data.error) pending.reject(new Error(data.error)); else pending.resolve(data.result);
    };
    formatterWorker.onerror = () => stopFormatter('Could not load formatting. Try again.');
  }
  return {formatDocument: (text, filename, cursorOffset) => new Promise((resolve, reject) => {
    const id = ++requestId;
    const timer = setTimeout(() => stopFormatter('Formatting timed out. Your draft is unchanged.'), 15000);
    pendingFormats.set(id, {resolve, reject, timer});
    formatterWorker.postMessage({id, text, filename, cursorOffset});
  })};
}

export function createEditor({element, filename, value, onChange = () => {}, formatter = loadFormatter}) {
  const states = new Map(), editable = new Compartment();
  let current = filename, destroyed = false, locked = false, formatting = false;
  const toolbar = document.createElement('div');
  toolbar.className = 'code-toolbar';
  const label = document.createElement('span'), formatButton = document.createElement('button');
  formatButton.type = 'button'; formatButton.className = 'inline-action'; formatButton.textContent = 'Format';
  formatButton.title = 'Format document (⌥⇧F / Alt+Shift+F)';
  toolbar.append(label, formatButton);
  const host = document.createElement('div'), status = document.createElement('p');
  host.className = 'code-surface'; status.className = 'code-status'; status.setAttribute('role', 'status');
  const hint = document.createElement('span');
  hint.className = 'sr-only'; hint.id = 'code-keyboard-help';
  hint.textContent = 'Tab indents. Shift Tab outdents. Press Escape, then Tab to leave the editor.';
  element.replaceChildren(toolbar, host, status, hint);
  function stateFor(name, text) {
    return EditorState.create({doc: text, extensions: [
      editable.of([EditorState.readOnly.of(locked), EditorView.editable.of(!locked)]),
      EditorState.tabSize.of(2), indentUnit.of('  '), EditorView.lineWrapping,
      history(), drawSelection(), lineNumbers(), highlightActiveLine(), bracketMatching(),
      languageSupport(name), syntaxHighlighting(highlighting),
      keymap.of([{key: 'Tab', run: tabIndent, shift: indentLess},
        {key: 'Escape', run: temporarilySetTabFocusMode},
        {key: 'Alt-Shift-f', run: () => {void format(); return true;}}, ...defaultKeymap, ...historyKeymap]),
      EditorView.contentAttributes.of({'aria-label': `Edit ${name}`, 'aria-describedby': hint.id, spellcheck: 'false'}),
      EditorView.updateListener.of(update => {if (update.docChanged) {status.textContent = ''; onChange(current, update.state.doc.toString());}}),
    ]});
  }
  const view = new EditorView({parent: host, state: stateFor(filename, value)});
  function paint() {
    label.textContent = {markdown: 'Markdown', yaml: 'YAML', json: 'JSON', text: 'Plain text'}[fileLanguage(current)];
    formatButton.hidden = fileLanguage(current) === 'text';
    formatButton.disabled = locked || formatting;
  }
  async function format() {
    if (locked || destroyed || formatting || fileLanguage(current) === 'text') return;
    const name = current, doc = view.state.doc, cursor = view.state.selection.main.head;
    formatting = true; status.textContent = 'Formatting…'; paint();
    try {
      const engine = await formatter();
      const result = await engine.formatDocument(doc.toString(), name, cursor);
      if (destroyed || current !== name || view.state.doc !== doc || locked) return;
      if (result.formatted !== doc.toString()) view.dispatch({changes: {from: 0, to: doc.length, insert: result.formatted},
        selection: {anchor: Math.min(result.cursorOffset, result.formatted.length)},
        annotations: [Transaction.userEvent.of('input.format'), isolateHistory.of('full')], scrollIntoView: true});
      status.textContent = 'Formatted'; view.focus();
    } catch (error) {
      if (!destroyed && current === name && view.state.doc === doc && !locked) status.textContent = error.message;
    } finally {formatting = false; if (!destroyed) {if (status.textContent === 'Formatting…') status.textContent = ''; paint();}}
  }
  formatButton.onclick = () => void format();
  paint();
  return {
    get value() {return view.state.doc.toString();},
    focus() {view.focus();},
    format,
    open(name, text) {
      if (locked || destroyed) return;
      states.set(current, {state: view.state, scroll: view.scrollDOM.scrollTop});
      current = name;
      const saved = states.get(name);
      view.setState(saved?.state || stateFor(name, text));
      view.scrollDOM.scrollTop = saved?.scroll || 0; status.textContent = ''; paint(); view.focus();
    },
    forget(name) {states.delete(name);},
    setReadOnly(value) {
      locked = value;
      view.dispatch({effects: editable.reconfigure([EditorState.readOnly.of(value), EditorView.editable.of(!value)])});
      paint();
    },
    destroy() {destroyed = true; view.destroy(); states.clear(); element.replaceChildren();},
  };
}
