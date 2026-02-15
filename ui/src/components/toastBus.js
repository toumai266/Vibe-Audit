let addToastGlobal = null;

export function setToastHandler(handler) {
  addToastGlobal = handler;
}

export function clearToastHandler(handler) {
  if (!handler || addToastGlobal === handler) {
    addToastGlobal = null;
  }
}

export function toast(message, type = 'info') {
  if (addToastGlobal) {
    addToastGlobal({ message, type, id: Date.now() });
  }
}
