import { SIZE, DIRECTIONS, SPEEDS, createGame, turn, step } from './engine.mjs';

const $ = id => document.getElementById(id);
const canvas = $('canvas');
const ctx = canvas.getContext('2d');
let game = createGame();
let timer = null;
let best = 0;
const storageKey = () => `snake-club-best-${$('difficulty').value}`;
const format = value => String(value).padStart(3, '0');

function loadBest() {
  best = 0;
  try {
    const value = Number(localStorage.getItem(storageKey()));
    if (Number.isSafeInteger(value) && value >= 0 && value <= (SIZE * SIZE - 4) * 10) best = value;
  } catch { /* Private browsing may disable storage; the game still works. */ }
}

function draw() {
  const cell = canvas.width / SIZE;
  ctx.fillStyle = '#19291f';
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  ctx.strokeStyle = '#26382a';
  ctx.lineWidth = 1;
  ctx.beginPath();
  for (let i = 1; i < SIZE; i++) {
    ctx.moveTo(i * cell, 0); ctx.lineTo(i * cell, canvas.height);
    ctx.moveTo(0, i * cell); ctx.lineTo(canvas.width, i * cell);
  }
  ctx.stroke();
  if (game.food) {
    const { x, y } = game.food;
    ctx.fillStyle = '#ff917b';
    ctx.beginPath(); ctx.roundRect(x * cell + 5, y * cell + 5, cell - 10, cell - 10, 6); ctx.fill();
    ctx.fillStyle = '#b6d98b'; ctx.fillRect(x * cell + 15, y * cell + 1, 4, 6);
  }
  for (let i = game.snake.length - 1; i >= 0; i--) {
    const { x, y } = game.snake[i];
    ctx.fillStyle = i === 0 ? '#ddff83' : '#aacb65';
    ctx.beginPath(); ctx.roundRect(x * cell + 2, y * cell + 2, cell - 4, cell - 4, i === 0 ? 7 : 4); ctx.fill();
  }
  const head = game.snake[0];
  const [dx, dy] = DIRECTIONS[game.direction];
  ctx.fillStyle = '#23351b';
  for (const side of [-1, 1]) {
    const x = head.x * cell + cell / 2 + dx * 6 + (dy ? side * 6 : 0);
    const y = head.y * cell + cell / 2 + dy * 6 + (dx ? side * 6 : 0);
    ctx.beginPath(); ctx.arc(x, y, 2.5, 0, Math.PI * 2); ctx.fill();
  }
}

function update() {
  if (game.score > best) {
    best = game.score;
    try { localStorage.setItem(storageKey(), String(best)); } catch { /* Keep the session record if storage is unavailable. */ }
  }
  $('score').textContent = format(game.score);
  $('best').textContent = format(best);
  $('length').textContent = `长度 ${game.snake.length}`;
  const labels = { ready: '待机中', running: '游戏中', paused: '已暂停', over: '游戏结束', won: '挑战成功' };
  $('phase').textContent = labels[game.phase];
  $('primary').textContent = game.phase === 'running' ? '暂停游戏' : game.phase === 'paused' ? '继续游戏' : game.phase === 'ready' ? '开始游戏' : '再来一局';
  $('overlay').hidden = game.phase === 'running';
  const copy = {
    ready: ['下一口，吃哪里？', '按空格键，或点击按钮开始。', '开始游戏', '准备好了，随时开始。'],
    paused: ['休息一下。', '蛇蛇在等你，准备好就继续。', '继续游戏', '已暂停，继续后从原位置出发。'],
    over: ['这一局，也很棒。', `本局 ${game.score} 分 · 个人纪录 ${best} 分`, '再来一局', `游戏结束，得到 ${game.score} 分。`],
    won: ['你填满了整个世界！', `满分 ${game.score}，每个转弯都恰到好处。`, '再来一局', `挑战成功，得到 ${game.score} 分！`],
  }[game.phase];
  if (copy) {
    $('overlay-title').textContent = copy[0];
    $('overlay-copy').textContent = copy[1];
    $('overlay-button').textContent = copy[2];
    $('status').textContent = copy[3];
  } else $('status').textContent = '保持专注，好戏才刚开始。';
  draw();
}

function stopClock() { clearInterval(timer); timer = null; }
function runClock() {
  stopClock();
  timer = setInterval(() => {
    step(game);
    if (game.phase !== 'running') stopClock();
    update();
  }, SPEEDS[$('difficulty').value]);
}
function restart(start = true) {
  stopClock();
  game = createGame();
  if (start) { game.phase = 'running'; runClock(); }
  update();
}
function toggle() {
  if (game.phase === 'running') { game.phase = 'paused'; stopClock(); update(); }
  else if (game.phase === 'paused') { game.phase = 'running'; runClock(); update(); }
  else restart();
}
function directionInput(direction) {
  if (game.phase === 'ready') restart();
  turn(game, direction);
}

$('primary').addEventListener('click', toggle);
$('overlay-button').addEventListener('click', toggle);
$('restart').addEventListener('click', () => restart());
$('difficulty').addEventListener('change', () => { loadBest(); restart(false); });
const keys = { ArrowUp: 'up', ArrowDown: 'down', ArrowLeft: 'left', ArrowRight: 'right', w: 'up', s: 'down', a: 'left', d: 'right' };
document.addEventListener('keydown', event => {
  if (event.altKey || event.ctrlKey || event.metaKey || /^(INPUT|SELECT|TEXTAREA)$/.test(event.target.tagName)) return;
  if (event.code === 'Space') {
    event.preventDefault();
    if (!event.repeat) toggle();
  } else {
    const direction = keys[event.key] || keys[event.key.toLowerCase()];
    if (direction) { event.preventDefault(); directionInput(direction); }
  }
});
document.querySelectorAll('[data-direction]').forEach(button => {
  button.addEventListener('click', () => directionInput(button.dataset.direction));
});
let touch = null;
canvas.addEventListener('pointerdown', event => {
  if (!event.isPrimary) return;
  touch = { x: event.clientX, y: event.clientY, id: event.pointerId };
  canvas.setPointerCapture(event.pointerId);
});
canvas.addEventListener('pointermove', event => {
  if (!touch || event.pointerId !== touch.id) return;
  const dx = event.clientX - touch.x, dy = event.clientY - touch.y;
  if (Math.max(Math.abs(dx), Math.abs(dy)) < 18) return;
  directionInput(Math.abs(dx) > Math.abs(dy) ? (dx > 0 ? 'right' : 'left') : (dy > 0 ? 'down' : 'up'));
  touch = { x: event.clientX, y: event.clientY, id: event.pointerId };
});
canvas.addEventListener('pointerup', () => { touch = null; });
canvas.addEventListener('pointercancel', () => { touch = null; });
function pauseWhenAway() { if (game.phase === 'running') toggle(); }
document.addEventListener('visibilitychange', () => { if (document.hidden) pauseWhenAway(); });
window.addEventListener('blur', pauseWhenAway);
loadBest();
update();
