// Marktcheck in der Angebotsübersicht
(() => {
  const nbsp = ' ';

  function badge(id, m) {
    const pct = m.diff_pct === null ? '–' : (m.diff_pct > 0 ? '+' : '') + m.diff_pct + nbsp + '%';
    const a = document.createElement('a');
    a.className = `mk mk-${m.verdict}`;
    a.href = `/markt/${id}`;
    a.title = m.label;
    a.textContent = pct;
    if (m.rough) { const s = document.createElement('sup'); s.textContent = '~'; a.appendChild(s); }
    return a;
  }

  document.querySelectorAll('[data-check]').forEach(btn => btn.addEventListener('click', async () => {
    const id = btn.dataset.check, cell = btn.closest('td');
    btn.disabled = true; btn.textContent = '…';
    try {
      const r = await fetch(`/markt/${id}`, {method: 'POST'});
      const m = await r.json();
      if (m.error) throw new Error(m.error);
      cell.replaceChildren(badge(id, m), btn);
      btn.textContent = '↻';
    } catch (e) {
      btn.textContent = 'Fehler'; btn.title = e.message;
    }
    btn.disabled = false;
  }));

  const all = document.getElementById('checkall');
  if (!all) return;
  async function poll() {
    const j = await (await fetch('/markt/status')).json();
    if (j.running) {
      all.disabled = true;
      all.textContent = `Marktcheck läuft … ${j.done}/${j.total}`;
      setTimeout(poll, 3000);
    } else if (all.disabled) {
      location.reload();
    }
  }
  all.addEventListener('click', async () => {
    await fetch('/markt/alle', {method: 'POST'});
    all.disabled = true;
    poll();
  });
  if (all.dataset.running === '1') poll();
})();
