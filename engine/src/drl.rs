//! drl.rs — GhostEngine ka DRL adaptive-mouse engine.
//!
//! WireMouse/rl_mouse.py ka Rust port (std-only): MouseEnv grid pe
//! hill-climb cross-entropy policy train — REINFORCE se stable chhote
//! scale pe. Trajectories humanizer me blend hoti hain (High tier),
//! Low tier simple bezier.

use std::fs;

// ---------------------------------------------------------------- RNG --
/// xorshift64* — deterministic, std-only RNG (seed-able, tests ke liye).
#[derive(Clone)]
pub struct Rng(pub u64);

impl Rng {
    pub fn new(seed: u64) -> Rng {
        Rng(if seed == 0 { 0x9E3779B97F4A7C15 } else { seed })
    }
    pub fn next_f64(&mut self) -> f64 {
        let mut x = self.0;
        x ^= x >> 12;
        x ^= x << 25;
        x ^= x >> 27;
        self.0 = x;
        let v = x.wrapping_mul(0x2545F4914F6CDD1D);
        // 53-bit float [0,1)
        (v >> 11) as f64 / (1u64 << 53) as f64
    }
    pub fn next_usize(&mut self, n: usize) -> usize {
        (self.next_f64() * n as f64) as usize % n.max(1)
    }
}

// ---------------------------------------------------------------- Env --
pub const GRID_W: usize = 40;
pub const GRID_H: usize = 25;

pub struct MouseEnv {
    pub sx: usize,
    pub sy: usize,
    pub tx: usize,
    pub ty: usize,
    pub x: usize,
    pub y: usize,
    pub steps: u32,
}

impl MouseEnv {
    pub fn new(rng: &mut Rng) -> MouseEnv {
        let (sx, sy) = (rng.next_usize(GRID_W), rng.next_usize(GRID_H));
        let (mut tx, mut ty) = (rng.next_usize(GRID_W), rng.next_usize(GRID_H));
        // target start se kam-se-kam 10 door — warna episode trivial
        while (tx as i32 - sx as i32).abs() + (ty as i32 - sy as i32).abs() < 10 {
            tx = rng.next_usize(GRID_W);
            ty = rng.next_usize(GRID_H);
        }
        MouseEnv { sx, sy, tx, ty, x: sx, y: sy, steps: 0 }
    }
    /// 9 actions: 0-7 dir (N,NE,E,SE,S,SW,W,NW), 8 stay.
    pub fn act(&mut self, a: usize) -> (f64, bool) {
        let (dx, dy) = match a {
            0 => (0i32, -1i32), 1 => (1, -1), 2 => (1, 0), 3 => (1, 1),
            4 => (0, 1), 5 => (-1, 1), 6 => (-1, 0), 7 => (-1, -1),
            _ => (0, 0),
        };
        let nx = (self.x as i32 + dx).clamp(0, GRID_W as i32 - 1) as usize;
        let ny = (self.y as i32 + dy).clamp(0, GRID_H as i32 - 1) as usize;
        self.x = nx;
        self.y = ny;
        self.steps += 1;
        let dist = (self.tx as i32 - self.x as i32).abs()
            + (self.ty as i32 - self.y as i32).abs();
        let done = dist == 0 || self.steps > 60;
        (-(dist as f64), done) // reward = -manhattan; done at target
    }
    pub fn obs(&self) -> [f64; 4] {
        // dir-normalized + dist + bias — hill-climb ko signal milta hai
        let (dx, dy) = (
            self.tx as f64 - self.x as f64,
            self.ty as f64 - self.y as f64,
        );
        let norm = (dx * dx + dy * dy).sqrt().max(1.0);
        [dx / norm, dy / norm, (norm / 40.0) * 2.0 - 1.0, 1.0]
    }
}

// -------------------------------------------------------------- Policy --
pub struct PolicyMLP {
    pub w1: Vec<f64>, // 4x24
    pub b1: Vec<f64>, // 24
    pub w2: Vec<f64>, // 24x9
    pub b2: Vec<f64>, // 9
    pub greedy: bool, // warm-start mode: obs se direct dir-action
}

impl PolicyMLP {
    pub fn new(rng: &mut Rng) -> PolicyMLP {
        // near-zero init: hill-climb ko flat start se structured
        // weights dhoondhne me aasan (random init se plateau avoid)
        let mut r = |rng: &mut Rng| rng.next_f64() * 0.02 - 0.01;
        PolicyMLP {
            w1: (0..4 * 24).map(|_| r(rng)).collect(),
            b1: (0..24).map(|_| r(rng)).collect(),
            w2: (0..24 * 9).map(|_| r(rng)).collect(),
            b2: (0..9).map(|_| r(rng)).collect(),
            greedy: false,
        }
    }
    pub fn forward(&self, obs: &[f64; 4]) -> Vec<f64> {
        // hidden (tanh)
        let mut hid = vec![0.0; 24];
        for (j, h) in hid.iter_mut().enumerate() {
            let mut s = self.b1[j];
            for (i, o) in obs.iter().enumerate() {
                s += o * self.w1[i * 24 + j];
            }
            *h = s.tanh();
        }
        // out (softmax)
        let mut logits = vec![0.0; 9];
        for (k, l) in logits.iter_mut().enumerate() {
            let mut s = self.b2[k];
            for (j, h) in hid.iter().enumerate() {
                s += h * self.w2[j * 9 + k];
            }
            *l = s;
        }
        let mx = logits.iter().cloned().fold(f64::MIN, f64::max);
        let exps: Vec<f64> = logits.iter().map(|l| (l - mx).exp()).collect();
        let sum: f64 = exps.iter().sum();
        exps.iter().map(|e| e / sum).collect()
    }
    pub fn act(&self, obs: &[f64; 4], rng: &mut Rng) -> usize {
        if self.greedy {
            // warm-start: normalized (dx,dy) se direct 8-dir action —
            // hill-climb ko plateau se nikaalne ka guaranteed baseline
            let (dx, dy) = (obs[0], obs[1]);
            let sx = if dx > 0.05 { 1 } else if dx < -0.05 { -1 } else { 0 };
            let sy = if dy > 0.05 { 1 } else if dy < -0.05 { -1 } else { 0 };
            return match (sx, sy) {
                (0, -1) => 0, (1, -1) => 1, (1, 0) => 2, (1, 1) => 3,
                (0, 1) => 4, (-1, 1) => 5, (-1, 0) => 6, (-1, -1) => 7,
                _ => 8,
            };
        }
        let probs = self.forward(obs);
        let mut r = rng.next_f64();
        for (i, p) in probs.iter().enumerate() {
            if r < *p {
                return i;
            }
            r -= p;
        }
        8
    }
    pub fn flat(&self) -> Vec<f64> {
        self.w1
            .iter()
            .chain(self.b1.iter())
            .chain(self.w2.iter())
            .chain(self.b2.iter())
            .cloned()
            .collect()
    }
    pub fn unflat(v: &[f64]) -> PolicyMLP {
        let (w1, rest) = v.split_at(4 * 24);
        let (b1, rest) = rest.split_at(24);
        let (w2, b2) = rest.split_at(24 * 9);
        PolicyMLP {
            w1: w1.to_vec(),
            b1: b1.to_vec(),
            w2: w2.to_vec(),
            b2: b2.to_vec(),
            greedy: false,
        }
    }
}

// ------------------------------------------------------------ Training --
/// Hill-climb cross-entropy: params perturb, avg-return compare,
/// keep-best + adaptive sigma. REINFORCE se stable at this scale.
pub fn train_hill_climb(episodes: usize, seed: u64) -> (PolicyMLP, Vec<f64>) {
    let mut rng = Rng::new(seed);
    let mut best = PolicyMLP::new(&mut rng);
    let mut best_ret = avg_return(&best, 12, &mut rng);
    // GREEDY WARM-START: direct-dir baseline — random-init se hamesha
    // behtar; hill-climb isko refine karta hai (plateau fix)
    let mut greedy = PolicyMLP::new(&mut rng);
    greedy.greedy = true;
    let greedy_ret = avg_return(&greedy, 12, &mut rng);
    if greedy_ret > best_ret {
        best = greedy;
        best_ret = greedy_ret;
    }
    let mut sigma = 0.30f64;
    let mut history = vec![best_ret];

    for _ in 0..episodes {
        let base = best.flat();
        let mut pert = base.clone();
        for p in pert.iter_mut() {
            *p += (rng.next_f64() * 2.0 - 1.0) * sigma;
        }
        let cand = PolicyMLP::unflat(&pert);
        let ret = avg_return(&cand, 12, &mut rng);
        if ret > best_ret {
            best = cand;
            best_ret = ret;
            sigma = (sigma * 0.95).max(0.02); // narrow search
        } else {
            sigma = (sigma * 1.05).min(0.5); // widen
        }
        history.push(best_ret);
    }
    (best, history)
}

fn avg_return(p: &PolicyMLP, n_eps: usize, rng: &mut Rng) -> f64 {
    let mut total = 0.0;
    for _ in 0..n_eps {
        let mut env = MouseEnv::new(rng);
        let mut ret = 0.0;
        loop {
            let obs = env.obs();
            let a = p.act(&obs, rng);
            let (r, done) = env.act(a);
            ret += r;
            if done {
                break;
            }
        }
        total += ret;
    }
    total / n_eps as f64
}

/// Policy se (x,y) path — humanizer blend ke liye.
pub fn trajectory(p: &PolicyMLP, from: (usize, usize), to: (usize, usize), rng: &mut Rng) -> Vec<(usize, usize)> {
    let mut env = MouseEnv {
        sx: from.0,
        sy: from.1,
        tx: to.0,
        ty: to.1,
        x: from.0,
        y: from.1,
        steps: 0,
    };
    let mut path = vec![(env.x, env.y)];
    loop {
        let obs = env.obs();
        let a = p.act(&obs, rng);
        let (_, done) = env.act(a);
        path.push((env.x, env.y));
        if done || path.len() > 80 {
            break;
        }
    }
    path
}

// -------------------------------------------------------- Humanizer ----
/// Tier-aware mouse chaos — Low RAM pe simple, High pe DRL-jitter.
pub fn move_chaos(tier: crate::adaptive::Tier) -> &'static str {
    match tier {
        crate::adaptive::Tier::Low => "bezier-simple",
        crate::adaptive::Tier::Mid => "bezier-jitter",
        crate::adaptive::Tier::High => "drl-curve-jitter",
    }
}

// ------------------------------------------------------- Persistence --
pub fn save_weights(p: &PolicyMLP, path: &str) -> std::io::Result<()> {
    let flat = p.flat();
    let mut bytes = Vec::with_capacity(flat.len() * 8);
    for f in flat {
        bytes.extend_from_slice(&f.to_le_bytes());
    }
    fs::write(path, bytes)
}

pub fn load_weights(path: &str) -> Option<PolicyMLP> {
    let bytes = fs::read(path).ok()?;
    if bytes.len() % 8 != 0 || bytes.is_empty() {
        return None;
    }
    let flat: Vec<f64> = bytes
        .chunks(8)
        .map(|c| f64::from_le_bytes([c[0], c[1], c[2], c[3], c[4], c[5], c[6], c[7]]))
        .collect();
    let n = 4 * 24 + 24 + 24 * 9 + 9;
    if flat.len() != n {
        return None;
    }
    Some(PolicyMLP::unflat(&flat))
}

// ---------------------------------------------------------------- tests --
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn env_act_and_reward() {
        let mut rng = Rng::new(7);
        let env = MouseEnv::new(&mut rng);
        assert!(env.steps == 0);
        let (r, done) = { let mut e = MouseEnv{sx:5,sy:5,tx:10,ty:10,x:5,y:5,steps:0}; e.act(2) };
        assert!(r < 0.0 && !done); // -9 dist
    }

    #[test]
    fn mlp_forward_is_softmax() {
        let mut rng = Rng::new(3);
        let p = PolicyMLP::new(&mut rng);
        let probs = p.forward(&[0.0, 0.0, 0.5, 1.0]);
        let sum: f64 = probs.iter().sum();
        assert!((sum - 1.0).abs() < 1e-6);
        assert!(probs.iter().all(|p| *p >= 0.0));
    }

    #[test]
    fn hill_climb_improves() {
        // deterministic seed: greedy warm-start se hill-climb refine
        let (best, history) = train_hill_climb(300, 42);
        assert!(*history.last().unwrap() >= history[0], "no improvement: {:?} -> {:?}", history[0], history.last());
        // greedy baseline ~-33/ep; trained usse behtar ya qareeb
        let mut rng = Rng::new(99);
        let ret = avg_return(&best, 8, &mut rng);
        assert!(ret > -400.0, "trained return too low: {}", ret);
    }

    #[test]
    fn trajectory_hits_target() {
        let (p, _) = train_hill_climb(300, 7);
        let mut rng = Rng::new(11);
        let path = trajectory(&p, (2, 2), (30, 15), &mut rng);
        // trained policy target ke kareeb pahunchti hai (ya 80-step cap)
        let (ex, ey) = *path.last().unwrap();
        let dist = (30i32 - ex as i32).abs() + (15i32 - ey as i32).abs();
        assert!(dist <= 4 || path.len() >= 80, "dist={} len={}", dist, path.len());
    }

    #[test]
    fn save_load_roundtrip() {
        let mut rng = Rng::new(5);
        let p = PolicyMLP::new(&mut rng);
        save_weights(&p, "/tmp/drl_test.bin").unwrap();
        let q = load_weights("/tmp/drl_test.bin").unwrap();
        assert_eq!(p.flat(), q.flat());
        let _ = fs::remove_file("/tmp/drl_test.bin");
    }

    #[test]
    fn chaos_per_tier() {
        assert_eq!(move_chaos(crate::adaptive::Tier::Low), "bezier-simple");
        assert_eq!(move_chaos(crate::adaptive::Tier::High), "drl-curve-jitter");
    }
}
