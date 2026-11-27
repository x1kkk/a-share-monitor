export const SIZE = 24;
export const DIRECTIONS = Object.freeze({ up: [0, -1], down: [0, 1], left: [-1, 0], right: [1, 0] });
export const SPEEDS = Object.freeze({ easy: 180, normal: 125, fast: 85 });
const same = (a, b) => a.x === b.x && a.y === b.y;

export function placeFood(snake, size = SIZE, random = Math.random) {
  const empty = [];
  for (let y = 0; y < size; y++) {
    for (let x = 0; x < size; x++) {
      if (!snake.some(segment => segment.x === x && segment.y === y)) empty.push({ x, y });
    }
  }
  return empty.length ? empty[Math.floor(random() * empty.length)] : null;
}

export function createGame(random = Math.random) {
  const snake = [12, 11, 10, 9].map(x => ({ x, y: 12 }));
  return { snake, direction: 'right', queue: [], food: placeFood(snake, SIZE, random), score: 0, phase: 'ready' };
}

export function turn(game, direction) {
  if (game.phase !== 'running' || !Object.hasOwn(DIRECTIONS, direction) || game.queue.length >= 2) return;
  const previous = game.queue.at(-1) || game.direction;
  const [dx, dy] = DIRECTIONS[previous];
  const [nx, ny] = DIRECTIONS[direction];
  if (previous !== direction && (dx + nx !== 0 || dy + ny !== 0)) game.queue.push(direction);
}

export function step(game, random = Math.random, size = SIZE) {
  if (game.phase !== 'running') return;
  game.direction = game.queue.shift() || game.direction;
  const [dx, dy] = DIRECTIONS[game.direction];
  const head = { x: game.snake[0].x + dx, y: game.snake[0].y + dy };
  const eating = game.food !== null && same(head, game.food);
  const body = eating ? game.snake : game.snake.slice(0, -1);
  if (head.x < 0 || head.x >= size || head.y < 0 || head.y >= size || body.some(segment => same(segment, head))) {
    game.phase = 'over';
    return;
  }
  game.snake.unshift(head);
  if (eating) {
    game.score += 10;
    game.food = placeFood(game.snake, size, random);
    if (game.food === null) game.phase = 'won';
  } else game.snake.pop();
}
