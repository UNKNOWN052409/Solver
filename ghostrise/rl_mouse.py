"""Deep RL mouse-movement agent - learns human-like trajectories from scratch.

Pure NumPy REINFORCE (policy-gradient), batch-updated, curriculum-trained.
No torch, no frameworks - runs anywhere Python+numpy runs, phones included.

Reward = human kinematics priors (motor-control literature):
    bell-shaped velocity profile, smooth late acceleration,
    micro-corrections near target, Fitts-law duration band,
    endpoint precision. Dense progress shaping during training.

Train:
    python3 -m ghostrise.rl_mouse train --episodes 800 --out rl_mouse.npz
Demo:
    python3 -m ghostrise.rl_mouse demo --weights rl_mouse.npz --tx 450 --ty 380
"""

import argparse
import json
from pathlib import Path

import numpy as np

ACTION_SET = np.array([(dx, dy) for dx in (-8, -4, 0, 4, 8)
                       for dy in (-8, -4, 0, 4, 8) if not (dx == 0 and dy == 0)],
                      dtype=np.float64)
N_ACTIONS = len(ACTION_SET)
MAX_STEPS = 128
STATE_DIM = 6


class TrajectoryEnv:
    """Start->target movement with curriculum difficulty and dense shaping."""

    def __init__(self, seed=None):
        self.rng = np.random.default_rng(seed)

    def reset(self, difficulty=1.0):
        """difficulty 0..1 : 0 = short easy moves, 1 = full range."""
        self.start = self.rng.uniform(0, 600, 2)
        ang = self.rng.uniform(0, 2 * np.pi)
        d_lo, d_hi = 40 + (120 - 40) * difficulty, 160 + (520 - 160) * difficulty
        dist = self.rng.uniform(d_lo, d_hi)
        self.target = self.start + np.array([np.cos(ang), np.sin(ang)]) * dist
        # keep target inside a sane viewport box
        self.target = np.clip(self.target, 0, 700)
        self.pos = self.start.copy()
        self.vel = np.zeros(2)
        self.vel_hist = []
        self.t = 0
        self.shape_bonus = 0.0
        self._prev_dist = float(np.hypot(*(self.target - self.pos)))
        return self._state()

    def _state(self):
        rel = self.target - self.pos
        # normalised features: raw +-520px inputs saturate tanh and kill learning
        return np.array([rel[0] / 300.0, rel[1] / 300.0,
                         self.vel[0] / 8.0, self.vel[1] / 8.0,
                         min(self.t / MAX_STEPS, 1.0),
                         np.hypot(*rel) / 600.0])

    def step(self, action_idx):
        delta = ACTION_SET[action_idx]
        rem = float(np.hypot(*(self.target - self.pos)))
        scale = max(0.25, min(1.0, rem / 140.0))
        applied = delta * scale
        self.pos += applied
        self.vel = applied.copy()
        self.vel_hist.append(float(np.hypot(*applied)))
        self.t += 1
        new_dist = float(np.hypot(*(self.target - self.pos)))
        # dense shaping: reward closing distance, punish drifting
        self.shape_bonus += (self._prev_dist - new_dist) * 0.05
        self._prev_dist = new_dist
        done = new_dist < 4 or self.t >= MAX_STEPS
        return self._state(), done

    def reward(self):
        pts_dist = float(np.hypot(*(self.target - self.pos)))
        v = np.array(self.vel_hist)
        n = len(v)
        if n < 6 or pts_dist >= 4:
            return -8.0 + self.shape_bonus

        peak_t = int(np.argmax(v)) / max(n - 1, 1)
        bell = max(-1.0, 1.0 - abs(peak_t - 0.45) * 2)

        acc = np.diff(v)
        late_var = float(np.var(acc[n // 2:])) if n > 4 else 9.9
        smooth = max(0.0, 1.0 - late_var / 6.0)

        corrections = int(np.sum(np.diff(np.sign(acc)) != 0))
        micro = 1.0 if corrections <= 2 else max(0.0, 1.0 - (corrections - 2) * 0.15)

        precision = 1.0  # we only reach here if pts_dist < 4
        d0 = float(np.hypot(*(self.target - self.start)))
        ideal_lo, ideal_hi = d0 / 14, d0 / 5
        dur = 1.0 if ideal_lo <= n <= ideal_hi else \
            max(0.0, 1.0 - abs(n - (ideal_lo + ideal_hi) / 2) / ideal_hi)

        return (2.0 * precision + 1.5 * dur + 1.2 * bell +
                1.0 * smooth + 0.8 * micro + self.shape_bonus)


# ---- policy network -------------------------------------------------------

class PolicyMLP:
    def __init__(self, hidden=32, seed=7):
        rng = np.random.default_rng(seed)
        s1, s2 = np.sqrt(1.0 / STATE_DIM), np.sqrt(1.0 / hidden)
        self.W1 = rng.normal(0, s1, (STATE_DIM, hidden))
        self.b1 = np.zeros(hidden)
        self.W2 = rng.normal(0, s2, (hidden, N_ACTIONS))
        self.b2 = np.zeros(N_ACTIONS)

    def forward(self, s):
        z1 = s @ self.W1 + self.b1
        h = np.tanh(z1)
        logits = h @ self.W2 + self.b2
        logits -= logits.max()
        p = np.exp(logits)
        p /= p.sum()
        return p, (s, h, p)

    def backward_step(self, cache, dlogp):
        _, h, _ = cache
        return {
            "W1": np.outer(cache[0], dlogp @ self.W2.T * (1 - h ** 2)),
            "b1": dlogp @ self.W2.T * (1 - h ** 2),
            "W2": np.outer(h, dlogp),
            "b2": dlogp,
        }

    def params(self):
        return {"W1": self.W1, "b1": self.b1, "W2": self.W2, "b2": self.b2}

    def set_params(self, p):
        self.W1, self.b1, self.W2, self.b2 = p["W1"], p["b1"], p["W2"], p["b2"]

    def act(self, state, greedy=False):
        p, _ = self.forward(state)
        if greedy:
            return int(np.argmax(p)), p
        return int(np.random.choice(N_ACTIONS, p=p)), p


def rollout_batch(policy, env, n_episodes, difficulty, entropy=0.0, greedy=False):
    """Collect a batch; returns per-episode data + aggregate stats."""
    batch = []
    successes = 0
    total_reward = 0.0
    for _ in range(n_episodes):
        state = env.reset(difficulty)
        caches, actions = [], []
        done = False
        while not done:
            probs, cache = policy.forward(state)
            if greedy:
                a = int(np.argmax(probs))
            else:
                # entropy-regularised sampling
                adj = probs ** (1.0 / (1.0 + entropy)) if entropy else probs
                adj = adj / adj.sum()
                a = int(np.random.choice(N_ACTIONS, p=adj))
            state, done = env.step(a)
            caches.append(cache)
            actions.append(a)
        R = env.reward()
        reached = float(np.hypot(*(env.target - env.pos))) < 4
        successes += reached
        total_reward += R
        batch.append((caches, actions, R))
    stats = {"avg_R": total_reward / n_episodes,
             "success_rate": successes / n_episodes}
    return batch, stats


def train(episodes=800, batch_size=16, lr=4e-3, out="rl_mouse.npz",
          log_every=10, seed=11):
    env = TrajectoryEnv(seed)
    policy = PolicyMLP(seed=seed)
    baseline = -8.0
    history = []
    n_batches = max(episodes // batch_size, 1)

    for b in range(1, n_batches + 1):
        frac = b / n_batches
        difficulty = min(1.0, frac * 0.9)          # curriculum ramp
        entropy = max(0.0, 0.5 * (1 - frac))
        lr_eff = lr * max(0.25, 1.0 - frac)          # decay as it converges
        batch, stats = rollout_batch(policy, env, batch_size,
                                     difficulty, entropy=entropy)
        baseline = 0.7 * baseline + 0.3 * stats["avg_R"]

        grads = {k: np.zeros_like(v) for k, v in policy.params().items()}
        for caches, actions, R in batch:
            adv = R - baseline
            discount = 1.0
            for cache_t, a_t in zip(caches, actions):
                dlogp = -cache_t[2].copy()         # softmax CE gradient
                dlogp[a_t] += 1.0
                sg = policy.backward_step(cache_t, dlogp)
                for k in grads:
                    grads[k] += sg[k] * adv * discount
                discount *= 0.97

        norm = max(len(batch), 1)
        for k in policy.params():
            policy.__dict__[k] += lr_eff * grads[k] / norm

        if b % log_every == 0:
            history.append({"batch": b, "avg_R": round(stats["avg_R"], 3),
                            "success": round(stats["success_rate"], 2),
                            "difficulty": round(difficulty, 2)})
            print(f"  batch {b:4d} | R={stats['avg_R']:+7.3f} | "
                  f"success={stats['success_rate']:.0%} | diff={difficulty:.2f}")

    np.savez(out, **policy.params(),
             meta=json.dumps({"history": history}))
    print(f"[+] saved -> {out}")
    return history


def sample_trajectory(weights_path, start=(30, 30), target=(450, 380)):
    data = np.load(weights_path, allow_pickle=True)
    p = {k: data[k] for k in ("W1", "b1", "W2", "b2")}
    policy = PolicyMLP()
    policy.set_params(p)

    class FixedEnv(TrajectoryEnv):
        def reset(self, difficulty=1.0):
            self.start = np.array(start, dtype=float)
            self.target = np.array(target, dtype=float)
            self.pos = self.start.copy()
            self.vel = np.zeros(2)
            self.vel_hist = []
            self.t = 0
            self.shape_bonus = 0.0
            self._prev_dist = float(np.hypot(*(self.target - self.pos)))
            return self._state()

    env = FixedEnv()
    state = env.reset()
    pts = [tuple(env.pos)]
    for _ in range(MAX_STEPS):
        a, _ = policy.act(state, greedy=True)
        state, done = env.step(a)
        pts.append(tuple(env.pos))
        if done:
            break
    err = float(np.hypot(*(env.target - env.pos)))
    return {"steps": len(pts) - 1, "endpoint_error_px": round(err, 2),
            "reached": err < 4,
            "path": [(round(x, 1), round(y, 1)) for x, y in pts[::4]]}


def main():
    ap = argparse.ArgumentParser(prog="rl_mouse", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    t = sub.add_parser("train")
    t.add_argument("--episodes", type=int, default=800)
    t.add_argument("--batch", type=int, default=16)
    t.add_argument("--lr", type=float, default=3e-2)
    t.add_argument("--out", default=str(Path(__file__).parent / "rl_mouse.npz"))
    t.set_defaults(fn=lambda a: train(a.episodes, a.batch, a.lr, a.out))

    d = sub.add_parser("demo")
    d.add_argument("--weights", default=str(Path(__file__).parent / "rl_mouse.npz"))
    d.add_argument("--sx", type=float, default=30)
    d.add_argument("--sy", type=float, default=30)
    d.add_argument("--tx", type=float, default=450)
    d.add_argument("--ty", type=float, default=380)
    d.set_defaults(fn=lambda a: print(
        json.dumps(sample_trajectory(a.weights, (a.sx, a.sy), (a.tx, a.ty)), indent=2)))

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
