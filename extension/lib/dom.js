// Minimal DOM helpers. Server data is only ever inserted as text (never innerHTML).

/** h('p', { class: 'msg', title: 'x' }, 'text', child, ...) */
export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'dataset') Object.assign(el.dataset, v);
    else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? '' : String(v));
  }
  append(el, children);
  return el;
}

function append(el, children) {
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
}

/** Replace an element's children. */
export function fill(el, ...children) {
  el.replaceChildren();
  append(el, children);
  return el;
}

export function $(id) {
  const el = document.getElementById(id);
  if (!el) throw new Error(`#${id} is missing`);
  return el;
}

export function show(el, visible = true) {
  el.hidden = !visible;
}

/** Write a status message: kind is '', 'ok', 'error' or 'busy'. */
export function message(el, text, kind = '') {
  el.textContent = text || '';
  el.className = `msg${kind ? ` ${kind}` : ''}`;
}
