import {formatWithCursor} from 'prettier/standalone';
import markdown from 'prettier/plugins/markdown';
import yaml from 'prettier/plugins/yaml';
import babel from 'prettier/plugins/babel';
import estree from 'prettier/plugins/estree';

export async function formatDocument(text, filename, cursorOffset = 0) {
  const parser = /\.(md|markdown)$/i.test(filename) ? 'markdown' : /\.ya?ml$/i.test(filename) ? 'yaml' : /\.json$/i.test(filename) ? 'json-stringify' : null;
  if (!parser) throw new Error('Formatting is available for Markdown, YAML, and JSON.');
  return formatWithCursor(text, {parser, plugins: parser === 'markdown' ? [markdown, yaml] : [yaml, babel, estree], cursorOffset,
    tabWidth: 2, useTabs: false, printWidth: 80, proseWrap: 'preserve', embeddedLanguageFormatting: 'auto'});
}
