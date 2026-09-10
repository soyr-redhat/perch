import {formatDocument} from './formatter.js';
self.onmessage = async ({data}) => {
  try {self.postMessage({id: data.id, result: await formatDocument(data.text, data.filename, data.cursorOffset)});}
  catch (error) {self.postMessage({id: data.id, error: error.message});}
};
