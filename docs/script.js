'use strict';
const scenarios = {
  single: { total: 349, detail: '1 kit principale', label: '1 pannello' },
  double: { total: 498, detail: '1 kit principale + 1 modulo aggiuntivo', label: '2 pannelli' },
  four: { total: 796, detail: '1 kit principale + 3 moduli aggiuntivi', label: '4 pannelli' }
};
const euro = value => new Intl.NumberFormat('it-IT', { useGrouping: 'always' }).format(value);
const kitPrice = value => `${euro(value.total)} €`;
const scenario = document.querySelector('#scenario');
const formPanel = document.querySelector('#form-panel');
const formKit = document.querySelector('#form-kit');
function updateBudget(value) {
  const data = scenarios[value];
  document.querySelector('#budget-total').textContent = kitPrice(data);
  document.querySelector('#budget-detail').textContent = data.detail;
}
scenario.addEventListener('change', () => {
  updateBudget(scenario.value);
  formKit.value = scenario.value;
  document.querySelector('#form-result').hidden = true;
});
document.querySelector('#budget-link').addEventListener('click', () => { formKit.value = scenario.value; });
formKit.addEventListener('change', () => {
  scenario.value = formKit.value;
  updateBudget(formKit.value);
});
updateBudget(scenario.value);

const menu = document.querySelector('.menu-toggle');
const nav = document.querySelector('#navigation');
function closeMenu() { nav.classList.remove('open'); menu.setAttribute('aria-expanded', 'false'); menu.setAttribute('aria-label', 'Apri menu'); }
menu.addEventListener('click', () => {
  const open = nav.classList.toggle('open');
  menu.setAttribute('aria-expanded', String(open));
  menu.setAttribute('aria-label', open ? 'Chiudi menu' : 'Apri menu');
});
nav.addEventListener('click', e => { if (e.target.closest('a')) closeMenu(); });
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeMenu(); });

const poses = {
  morning: { sun: 'translate(112 112)', panel: 'rotate(-12 300 168)', text: 'Il pannello si orienta verso il sole del mattino.' },
  noon: { sun: 'translate(300 55)', panel: 'rotate(0 300 168)', text: 'A mezzogiorno cambia inclinazione seguendo la posizione del sole.' },
  afternoon: { sun: 'translate(488 112)', panel: 'rotate(12 300 168)', text: 'Nel pomeriggio si orienta verso ovest. La simulazione non misura la produzione.' }
};
document.querySelectorAll('[data-time]').forEach(button => button.addEventListener('click', () => {
  document.querySelectorAll('[data-time]').forEach(item => {
    const active = item === button;
    item.classList.toggle('selected', active);
    item.setAttribute('aria-pressed', String(active));
  });
  const pose = poses[button.dataset.time];
  document.querySelector('#demo-sun').setAttribute('transform', pose.sun);
  document.querySelector('#demo-panel').setAttribute('transform', pose.panel);
  document.querySelector('#demo-caption').textContent = pose.text;
}));

const form = document.querySelector('#assessment-form');
let summary = '';
form.addEventListener('input', () => { document.querySelector('#form-result').hidden = true; });
form.addEventListener('submit', event => {
  event.preventDefault();
  const values = new FormData(form);
  const chosen = scenarios[values.get('kit')];
  const panelLabel = formPanel.selectedOptions[0].textContent;
  const power = values.get('power');
  summary = [
    'SOLAR TRACKER — RIEPILOGO DELLA CONFIGURAZIONE',
    'Proposta dimostrativa di servizio domestico in stile COESA Energy.',
    '',
    `Pannello: ${panelLabel}`,
    `Configurazione: ${chosen.label} — ${chosen.detail}`,
    `Spazio: ${values.get('space')}`,
    `Età: ${values.get('age')}`,
    `Potenza nominale: ${power ? power + ' W' : 'Da verificare'}`,
    `Dettagli: ${values.get('notes') || 'Nessun dettaglio aggiuntivo'}`,
    '',
    `STIMA DEI KIT: ${kitPrice(chosen)} — IVA ESCLUSA.`,
    'Fonte: analisi economica HackTurin di Davide Colabella, 11 ottobre 2026, pagine 5–8.',
    'Prezzi ipotizzati per la produzione in serie, con pannelli già di proprietà del cliente. Non costituiscono listino, offerta o preventivo.',
    'Il kit principale prevede batteria di servizio, regolatore, camera e irrigazione; i moduli aggiuntivi dipendono dal principale.',
    'Software opzionale: 48 €/anno, IVA esclusa, non incluso nel totale.',
    'Da quotare separatamente: verifica dei pannelli, smontaggio, reinstallazione e collaudo sul posto, opere, inverter, connessione alla rete, pratiche ed eventuali pannelli forniti con il kit.',
    'Il prezzo finale al consumatore dovrà includere l’IVA applicabile e gli interventi concordati.',
    '',
    'DA VERIFICARE: stato e prestazioni del pannello, dimensioni e peso, supporto meccanico, esposizione, ancoraggio e vento, componenti elettrici e utilizzo dell’energia.',
    'Solar Tracker nasce da un prototipo. La versione domestica e le dotazioni commerciali sono da validare.',
    '',
    'Questi dati sono stati elaborati soltanto nel browser. Nessuna richiesta è stata inviata a COESA o ad altri destinatari.'
  ].join('\n');
  document.querySelector('#result-description').textContent = `${chosen.label} · ${values.get('space')} · stima dei kit ${kitPrice(chosen)}, IVA esclusa. Pannelli, software opzionale e interventi sul posto non inclusi nel totale. Nessuna richiesta inviata.`;
  document.querySelector('#form-result').hidden = false;
});
document.querySelector('#download-summary').addEventListener('click', () => {
  if (!summary) return;
  const url = URL.createObjectURL(new Blob([summary], { type: 'text/plain;charset=utf-8' }));
  const link = document.createElement('a');
  link.href = url;
  link.download = 'solar-tracker-riepilogo.txt';
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
});
document.querySelector('#edit-summary').addEventListener('click', () => {
  document.querySelector('#form-result').hidden = true;
  formPanel.focus();
});
