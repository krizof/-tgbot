"use client";

import { useCallback, useEffect, useRef, useState } from "react";

type Mode = "snake" | "tap";
type Screen = "menu" | Mode | "result";

declare global {
  interface Window {
    Telegram?: { WebApp: {
      ready(): void; expand(): void; sendData(data: string): void;
      HapticFeedback?: { impactOccurred(style: string): void };
    }};
  }
}

const MODE_LABELS: Record<Mode, string> = { snake: "Змейка", tap: "Тап-спринт" };

function SnakeGame({ onFinish }: { onFinish: (score: number) => void }) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const snake = useRef([{ x: 7, y: 8 }, { x: 6, y: 8 }, { x: 5, y: 8 }]);
  const food = useRef({ x: 11, y: 8 });
  const direction = useRef({ x: 1, y: 0 });
  const nextDirection = useRef({ x: 1, y: 0 });
  const score = useRef(0);
  const [shownScore, setShownScore] = useState(0);
  const [running, setRunning] = useState(false);

  const draw = useCallback(() => {
    const canvas = canvasRef.current;
    const ctx = canvas?.getContext("2d");
    if (!canvas || !ctx) return;
    const cell = canvas.width / 16;
    ctx.fillStyle = "#b7c89a";
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.strokeStyle = "rgba(32,46,31,.08)";
    for (let i = 0; i <= 16; i += 1) {
      ctx.beginPath(); ctx.moveTo(i * cell, 0); ctx.lineTo(i * cell, canvas.height); ctx.stroke();
      ctx.beginPath(); ctx.moveTo(0, i * cell); ctx.lineTo(canvas.width, i * cell); ctx.stroke();
    }
    ctx.fillStyle = "#203020";
    snake.current.forEach((part, index) => {
      ctx.fillRect(part.x * cell + 2, part.y * cell + 2, cell - 4, cell - 4);
      if (index === 0) {
        ctx.fillStyle = "#d9e4bd";
        ctx.fillRect(part.x * cell + cell * .63, part.y * cell + cell * .25, 3, 3);
        ctx.fillStyle = "#203020";
      }
    });
    ctx.fillStyle = "#6f2c2c";
    ctx.beginPath();
    ctx.arc((food.current.x + .5) * cell, (food.current.y + .5) * cell, cell * .32, 0, Math.PI * 2);
    ctx.fill();
  }, []);

  const reset = useCallback(() => {
    snake.current = [{ x: 7, y: 8 }, { x: 6, y: 8 }, { x: 5, y: 8 }];
    food.current = { x: 11, y: 8 };
    direction.current = { x: 1, y: 0 };
    nextDirection.current = { x: 1, y: 0 };
    score.current = 0;
    setShownScore(0);
    setRunning(true);
  }, []);

  const turn = useCallback((x: number, y: number) => {
    if (direction.current.x + x === 0 && direction.current.y + y === 0) return;
    nextDirection.current = { x, y };
    window.Telegram?.WebApp.HapticFeedback?.impactOccurred("light");
  }, []);

  useEffect(() => { draw(); }, [draw]);
  useEffect(() => {
    if (!running) return;
    const timer = window.setInterval(() => {
      direction.current = nextDirection.current;
      const head = snake.current[0];
      const next = { x: head.x + direction.current.x, y: head.y + direction.current.y };
      const crashed = next.x < 0 || next.x >= 16 || next.y < 0 || next.y >= 16
        || snake.current.some((part) => part.x === next.x && part.y === next.y);
      if (crashed) {
        window.clearInterval(timer); setRunning(false); onFinish(score.current); return;
      }
      snake.current.unshift(next);
      if (next.x === food.current.x && next.y === food.current.y) {
        score.current += 10; setShownScore(score.current);
        let candidate = food.current;
        do { candidate = { x: Math.floor(Math.random() * 16), y: Math.floor(Math.random() * 16) }; }
        while (snake.current.some((part) => part.x === candidate.x && part.y === candidate.y));
        food.current = candidate;
      } else snake.current.pop();
      draw();
    }, Math.max(85, 175 - score.current));
    return () => window.clearInterval(timer);
  }, [running, draw, onFinish]);

  return <section className="game-panel">
    <div className="score-strip"><span>SCORE</span><strong>{String(shownScore).padStart(4, "0")}</strong></div>
    <canvas ref={canvasRef} className="snake-board" width={320} height={320} aria-label="Поле игры Змейка" />
    {!running && <button className="primary" onClick={reset}>Начать игру</button>}
    {running && <div className="dpad" aria-label="Управление змейкой">
      <button onClick={() => turn(0, -1)}>▲</button>
      <div><button onClick={() => turn(-1, 0)}>◀</button><button onClick={() => turn(1, 0)}>▶</button></div>
      <button onClick={() => turn(0, 1)}>▼</button>
    </div>}
  </section>;
}

function TapGame({ onFinish }: { onFinish: (score: number) => void }) {
  const [running, setRunning] = useState(false);
  const [seconds, setSeconds] = useState(10);
  const [score, setScore] = useState(0);
  const scoreRef = useRef(0);

  useEffect(() => {
    if (!running) return;
    const timer = window.setInterval(() => setSeconds((value) => {
      if (value <= 1) {
        window.clearInterval(timer); setRunning(false);
        window.setTimeout(() => onFinish(scoreRef.current), 0); return 0;
      }
      return value - 1;
    }), 1000);
    return () => window.clearInterval(timer);
  }, [running, onFinish]);

  const start = () => { scoreRef.current = 0; setScore(0); setSeconds(10); setRunning(true); };
  const tap = () => {
    if (!running) return;
    scoreRef.current += 1; setScore(scoreRef.current);
    window.Telegram?.WebApp.HapticFeedback?.impactOccurred("light");
  };
  return <section className="game-panel tap-game">
    <div className="score-strip"><span>TIME {seconds}</span><strong>{String(score).padStart(3, "0")}</strong></div>
    <button className={`tap-target ${running ? "active" : ""}`} onClick={tap} disabled={!running}>{running ? "ЖМИ!" : "ГОТОВ?"}</button>
    {!running && <button className="primary" onClick={start}>Старт — 10 секунд</button>}
  </section>;
}

export default function Home() {
  const [screen, setScreen] = useState<Screen>("menu");
  const [mode, setMode] = useState<Mode>("snake");
  const [result, setResult] = useState(0);
  useEffect(() => { window.Telegram?.WebApp.ready(); window.Telegram?.WebApp.expand(); }, []);
  const choose = (nextMode: Mode) => { setMode(nextMode); setScreen(nextMode); };
  const finish = useCallback((score: number) => { setResult(score); setScreen("result"); }, []);
  const submit = () => {
    const webApp = window.Telegram?.WebApp;
    if (!webApp) { window.alert("Открой игру кнопкой внутри Telegram, чтобы сохранить результат."); return; }
    webApp.sendData(JSON.stringify({ mode, score: result }));
  };
  return <main>
    <header className="brand"><span className="signal">▮▮▮</span><h1>FRIENDS ARCADE</h1><span>●</span></header>
    {screen === "menu" && <section className="menu">
      <p className="eyebrow">ВЫБЕРИ РЕЖИМ</p>
      <button className="mode-card" onClick={() => choose("snake")}><span>🐍</span><div><strong>ЗМЕЙКА</strong><small>Еда +10 · не врежься</small></div><b>›</b></button>
      <button className="mode-card" onClick={() => choose("tap")}><span>⚡</span><div><strong>ТАП-СПРИНТ</strong><small>10 секунд · максимум тапов</small></div><b>›</b></button>
      <div className="hint">Рекорд сохранится в общем рейтинге бота</div>
    </section>}
    {screen === "snake" && <SnakeGame onFinish={finish} />}
    {screen === "tap" && <TapGame onFinish={finish} />}
    {screen === "result" && <section className="result-card">
      <span className="trophy">🏆</span><p>{MODE_LABELS[mode]}</p><h2>{result}</h2><small>ОЧКОВ</small>
      <button className="primary" onClick={submit}>Сохранить рекорд</button>
      <button className="secondary" onClick={() => setScreen(mode)}>Сыграть ещё</button>
      <button className="text-button" onClick={() => setScreen("menu")}>← К выбору игр</button>
    </section>}
    {screen !== "menu" && screen !== "result" && <button className="back" onClick={() => setScreen("menu")}>← Меню</button>}
    <footer>PRIVATE ARCADE · 2026</footer>
  </main>;
}
