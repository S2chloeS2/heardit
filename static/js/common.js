/* Shared helpers: toasts, fetch wrapper, tiny markdown renderer. */

function toast(message, kind = '') {
  const host = document.getElementById('toasts');
  if (!host) return;
  const el = document.createElement('div');
  el.className = 'toast' + (kind ? ` toast-${kind}` : '');
  el.textContent = message;
  host.appendChild(el);
  setTimeout(() => el.remove(), kind === 'bad' ? 7000 : 4000);
}

/** fetch that turns non-2xx JSON {error} into a thrown Error. */
async function api(url, options = {}) {
  const res = await fetch(url, options);
  let data = null;
  try {
    data = await res.json();
  } catch {
    /* empty or non-JSON body */
  }
  if (!res.ok) {
    const err = new Error((data && data.error) || `${(window.I18N||{}).requestFailed || '요청이 실패했습니다'} (${res.status})`);
    // A 403 from a plan gate carries where to upgrade.
    if (data && data.upgrade) err.upgrade = data.upgrade;
    throw err;
  }
  return data;
}

function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}

/** Enough markdown for the summaries the model returns. */
function renderMarkdown(src) {
  const lines = escapeHtml(src || '').split('\n');
  const out = [];
  let inList = false;

  const closeList = () => {
    if (inList) {
      out.push('</ul>');
      inList = false;
    }
  };

  for (const raw of lines) {
    const line = raw.trimEnd();
    const heading = line.match(/^(#{1,4})\s+(.*)$/);
    const bullet = line.match(/^\s*[-*]\s+(.*)$/);

    if (heading) {
      closeList();
      out.push(`<h3>${inline(heading[2])}</h3>`);
    } else if (bullet) {
      if (!inList) {
        out.push('<ul>');
        inList = true;
      }
      out.push(`<li>${inline(bullet[1])}</li>`);
    } else if (!line.trim()) {
      closeList();
    } else {
      closeList();
      out.push(`<p>${inline(line)}</p>`);
    }
  }
  closeList();
  return out.join('');

  function inline(text) {
    return text
      .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
      .replace(/`(.+?)`/g, '<code>$1</code>');
  }
}

function timeNow() {
  return new Date().toTimeString().slice(0, 5);
}
