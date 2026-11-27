import test from 'node:test';
import assert from 'node:assert/strict';
import { SIZE, createGame, placeFood, turn, step } from './engine.mjs';

const running = () => ({ ...createGame(() => 0), phase: 'running' });
test('moving keeps length; eating grows and scores without placing food on the snake', () => {
  const game = running();
  step(game);
  assert.deepEqual(game.snake[0], { x: 13, y: 12 });
  assert.equal(game.snake.length, 4);
  game.food = { x: 14, y: 12 };
  step(game, () => 0);
  assert.equal(game.score, 10);
  assert.equal(game.snake.length, 5);
  assert.ok(!game.snake.some(p => p.x === game.food.x && p.y === game.food.y));
});
test('turn queue rejects reversals, including rapid input, and applies one turn per tick', () => {
  const game = running();
  turn(game, 'left'); turn(game, 'up'); turn(game, 'down'); turn(game, 'left'); turn(game, 'down');
  assert.deepEqual(game.queue, ['up', 'left']);
  step(game); assert.equal(game.direction, 'up');
  step(game); assert.equal(game.direction, 'left');
  turn(game, 'constructor'); assert.deepEqual(game.queue, []);
});
test('wall and body collisions end a round, but a departing tail cell is legal', () => {
  const wall = running(); wall.snake[0] = { x: SIZE - 1, y: 12 }; step(wall); assert.equal(wall.phase, 'over');
  const game = running();
  game.snake = [{ x: 1, y: 1 }, { x: 1, y: 2 }, { x: 2, y: 2 }, { x: 2, y: 1 }];
  game.food = { x: 0, y: 0 }; step(game); assert.equal(game.phase, 'running');
  const body = running(); body.snake = [{ x: 1, y: 1 }, { x: 2, y: 1 }, { x: 2, y: 2 }, { x: 1, y: 2 }];
  step(body); assert.equal(body.phase, 'over'); assert.equal(body.snake.length, 4);
});
test('paused and finished rounds cannot move; new rounds reset state', () => {
  for (const phase of ['ready', 'paused', 'over', 'won']) {
    const game = { ...running(), phase }; const before = structuredClone(game);
    turn(game, 'up'); step(game); assert.deepEqual(game, before);
  }
  assert.equal(createGame().score, 0); assert.equal(createGame().phase, 'ready');
});
test('eating the final empty cell wins without an infinite food search', () => {
  const game = running();
  game.snake = [{ x: 0, y: 0 }, { x: 0, y: 1 }, { x: 1, y: 1 }];
  game.food = { x: 1, y: 0 };
  step(game, () => 0, 2);
  assert.equal(game.phase, 'won'); assert.equal(game.food, null); assert.equal(game.score, 10);
  assert.equal(placeFood(game.snake, 2), null);
});
