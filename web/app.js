'use strict';

const API = '/.netlify/functions/settings';
const loginView = document.getElementById('login-view');
const dashboardView = document.getElementById('dashboard-view');
const loginForm = document.getElementById('login-form');
const settingsForm = document.getElementById('settings-form');
const message = document.getElementById('message');
const passwordInput = document.getElementById('password');
const walkRange = document.getElementById('walk-range');
const walkValue = document.getElementById('walk-value');
const budget = document.getElementById('budget-input');
const ownerOnly = document.getElementById('owner-only');
const watching = document.getElementById('watching');
const saveButton = document.getElementById('save-button');
const loginButton = document.getElementById('login-button');
const lastScan = document.getElementById('last-scan');
const checkStatus = document.getElementById('check-status');
let password = sessionStorage.getItem('minsk_dashboard_auth') || '';
let baseline = null;
let revision = null;
let busy = false;

function feedback(text, error = false) {
  message.textContent = text;
  message.classList.toggle('error', error);
}

async function api(method, payload) {
  const response = await fetch(API, {
    method,
    headers: {
      Authorization: `Bearer ${password}`,
      ...(payload ? { 'Content-Type': 'application/json' } : {}),
    },
    ...(payload ? { body: JSON.stringify(payload) } : {}),
    cache: 'no-store',
  });
  let data = {};
  try { data = await response.json(); } catch { /* no JSON */ }
  if (!response.ok) {
    const error = new Error(data.error || 'Не удалось связаться с сервером.');
    error.status = response.status;
    throw error;
  }
  return data;
}

function settingsFromForm() {
  if (budget.value !== '' && (!budget.validity.valid || !/^\d+$/.test(budget.value))) {
    throw new Error('Введи корректный бюджет от 0 до 100000 $.');
  }
  const chosenRooms = document.querySelector('input[name="rooms"]:checked');
  return {
    walk_minutes: Number(walkRange.value),
    max_price_usd: budget.value === '' ? 0 : Number(budget.value),
    rooms: Number(chosenRooms.value),
    owner_only: ownerOnly.checked,
    watching: watching.checked,
  };
}

function hydrateForm(settings) {
  walkRange.value = String(settings.walk_minutes);
  budget.value = settings.max_price_usd ? String(settings.max_price_usd) : '';
  ownerOnly.checked = settings.owner_only;
  watching.checked = settings.watching;
  document.querySelector(`input[name="rooms"][value="${settings.rooms}"]`).checked = true;
  draw();
}

function draw() {
  walkValue.textContent = walkRange.value;
  const percent = ((Number(walkRange.value) - 5) / 40) * 100;
  walkRange.style.setProperty('--range-progress', `${percent}%`);
  if (!baseline) return;
  try {
    const hasChanges = JSON.stringify(settingsFromForm()) !== JSON.stringify(baseline);
    saveButton.disabled = busy || !hasChanges;
  } catch {
    saveButton.disabled = busy;
  }
}

function loggedOut() {
  dashboardView.hidden = true;
  loginView.hidden = false;
  baseline = null;
  revision = null;
  password = '';
  sessionStorage.removeItem('minsk_dashboard_auth');
  passwordInput.value = '';
}

async function openDashboard() {
  busy = true;
  loginButton.disabled = true;
  feedback('');
  try {
    const data = await api('GET');
    baseline = data.settings;
    revision = data.revision;
    hydrateForm(baseline);
    checkStatus.textContent = data.pending
      ? 'Изменения ожидают следующей проверки.'
      : 'Новые настройки действуют при следующей проверке.';
    if (data.last_scan_at) {
      const stamp = new Date(data.last_scan_at);
      if (!Number.isNaN(stamp.getTime())) {
        lastScan.textContent = `Последняя проверка: ${stamp.toLocaleString('ru-RU', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' })}`;
      }
    }
    sessionStorage.setItem('minsk_dashboard_auth', password);
    loginView.hidden = true;
    dashboardView.hidden = false;
  } catch (error) {
    if (error.status === 401) {
      loggedOut();
      feedback('Неверный пароль.', true);
    } else {
      feedback(error.message, true);
    }
  } finally {
    busy = false;
    loginButton.disabled = false;
    draw();
  }
}

loginForm.addEventListener('submit', async event => {
  event.preventDefault();
  password = passwordInput.value;
  if (!password) return;
  await openDashboard();
});

document.getElementById('logout-button').addEventListener('click', () => {
  loggedOut();
  feedback('');
});

settingsForm.addEventListener('input', () => { draw(); feedback(''); });
settingsForm.addEventListener('change', () => { draw(); feedback(''); });
settingsForm.addEventListener('submit', async event => {
  event.preventDefault();
  if (busy) return;
  let proposed;
  try { proposed = settingsFromForm(); }
  catch (error) { feedback(error.message, true); return; }
  busy = true;
  saveButton.disabled = true;
  saveButton.textContent = 'Сохранение…';
  feedback('');
  try {
    const result = await api('PUT', { settings: proposed, expectedRevision: revision });
    revision = result.revision;
    baseline = result.settings;
    hydrateForm(baseline);
    checkStatus.textContent = 'Изменения ожидают следующей проверки.';
    feedback('Сохранено. Бот применит настройки при следующей проверке.');
  } catch (error) {
    if (error.status === 401) {
      loggedOut();
      feedback('Сессия закончилась. Войди снова.', true);
    } else if (error.status === 409) {
      feedback('Настройки изменились в другом окне. Обнови страницу.', true);
    } else {
      feedback(error.message, true);
    }
  } finally {
    busy = false;
    saveButton.textContent = 'Сохранить настройки';
    draw();
  }
});

if (password) openDashboard();
