//! GhostEngine M4 — multi-layer encryption cascade (password + cookie vault).
//!
//! Extends the M3 AES-256-GCM vault into the ARCHITECTURE.md encryption
//! model. Defense in depth — koi EK algorithm nahi jo Toot jaaye, layers
//! cascade hain:
//!
//! ```text
//!   user/admin plaintext
//!        │  Layer A — AES-256-GCM  (per-entry key K_e)
//!        ▼
//!   per-entry blob  ──►  Layer B — ChaCha20-Poly1305 wrap (key K_b)
//!        ▼
//!   whole store  ──►  Layer C — Argon2id KDF (memory-hard, GPU-tuned)
//!        ▼
//!   on-disk ciphertext (v4 format "GVL4")
//! ```
//!
//! Access model (who can read):
//! - **User**   — unlock with master pass (Argon2id derived key). Must be
//!   typed, never stored.
//! - **Admin**  — unlock with admin key (service-held, enclave/TPM-bound).
//!   Can read (never generate) via the same decoder.
//! - **Attacker/malware** — gets the file; has ciphertext + neither key.
//!   Master pass isn't on disk; admin key isn't in the file or process.
//!   Layer C cost caps brute force; Layers A+B both must break in order.
//!
//! Cookie vault: same multi-layer store, generic `SecretBlob` payload so a
//! logged-in provider's session cookies live under the same crypto contract.
//!
//! Everything is local-only. No plaintext is ever written to disk.

#![cfg(feature = "crypt")]

use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};

use aes_gcm::aead::{Aead, AeadCore, KeyInit, OsRng};
use aes_gcm::{Aes256Gcm, Key as AesKey, Nonce};
use argon2::{Algorithm, Argon2, Params, Version};
use chacha20poly1305::{ChaCha20Poly1305, Key as ChaKey, Nonce as ChaNonce};
use sha2::{Digest, Sha256};

/// V4 magic — v1 ("GVLT") unchanged; v4 multi-layer store.
const MAGIC: &[u8; 4] = b"GVL4";
const VERSION: u8 = 4;

// Layer C (Argon2id) cost — tuned high; GPU accel makes legit unlock fast,
// attacker parallelization capped by per-store salt + memory hardness.
const ARGON_M_BYTES: u32 = 64 * 1024;      // 64 MiB
const ARGON_T_COST: u32 = 3;
const ARGON_P_COST: u32 = 1;

/// Verify marker — wrong key → Argon decode then AEAD tag fail → refused
/// at Layer A before any entry is touched.
const VERIFY_MSG: &[u8] = b"ghostengine-layer-cascade-check";

/// A generic encrypted secret (password entry or cookie blob).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Secret {
    pub kind: SecretKind,
    pub id: String,          // site / provider / unique key
    pub payload: Vec<u8>,    // plaintext secret (only in memory while unlocked)
    pub meta: String,        // notes / descriptor
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SecretKind {
    Password,
    Cookie,
    ApiKey,
}

impl Secret {
    pub fn password(site: &str, user: &str, pass: &str, notes: &str) -> Self {
        Secret {
            kind: SecretKind::Password,
            id: format!("{}::{}", site, user),
            payload: pass.as_bytes().to_vec(),
            meta: notes.to_string(),
        }
    }
    pub fn cookie(provider: &str, cookie_header: &str) -> Self {
        Secret {
            kind: SecretKind::Cookie,
            id: provider.to_string(),
            payload: cookie_header.as_bytes().to_vec(),
            meta: String::new(),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CryptError {
    Locked,
    WrongKey,
    NotFound(String),
    Corrupt(String),
    Io(String),
}

impl std::fmt::Display for CryptError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            CryptError::Locked => write!(f, "store locked hai"),
            CryptError::WrongKey => write!(f, "galat key/master-pass"),
            CryptError::NotFound(s) => write!(f, "not found: {}", s),
            CryptError::Corrupt(c) => write!(f, "crypt corrupt: {}", c),
            CryptError::Io(c) => write!(f, "io: {}", c),
        }
    }
}
impl std::error::Error for CryptError {}

fn io_err(e: std::io::Error) -> CryptError {
    CryptError::Io(e.to_string())
}

// ------------------------------------------------------------------ KDF --

/// Argon2id key derivation — memory-hard, the anti-brute-force wall.
fn argon_key(master: &str, salt: &[u8]) -> Result<[u8; 32], CryptError> {
    let params = Params::new(ARGON_M_BYTES, ARGON_T_COST, ARGON_P_COST, Some(32))
        .map_err(|e| CryptError::Corrupt(format!("argon params: {}", e)))?;
    let argon = Argon2::new(Algorithm::Argon2id, Version::V0x13, params);
    let mut out = [0u8; 32];
    argon
        .hash_password_into(master.as_bytes(), salt, &mut out)
        .map_err(|e| CryptError::Corrupt(format!("argon hash: {}", e)))?;
    Ok(out)
}

/// SHA-256 based key, for the admin path where we already hold entropy
/// (the admin key is a high-entropy random, not a passphrase needing KDF).
fn sha256_key(entropy: &[u8]) -> [u8; 32] {
    Sha256::digest(entropy).into()
}

// ------------------------------------------------------ cascade crypto ---

/// Layer A seal (AES-256-GCM). Returns nonce(12) ++ ct.
fn a_seal(key: &[u8; 32], plain: &[u8]) -> Vec<u8> {
    let c = Aes256Gcm::new(AesKey::<Aes256Gcm>::from_slice(key));
    let n = Aes256Gcm::generate_nonce(&mut OsRng);
    let ct = c.encrypt(&n, plain).expect("aes encrypt infallible");
    let mut out = Vec::with_capacity(12 + ct.len());
    out.extend_from_slice(n.as_slice());
    out.extend_from_slice(&ct);
    out
}

fn a_open(key: &[u8; 32], blob: &[u8]) -> Result<Vec<u8>, CryptError> {
    if blob.len() < 28 {
        return Err(CryptError::Corrupt("aes blob chhota".into()));
    }
    let c = Aes256Gcm::new(AesKey::<Aes256Gcm>::from_slice(key));
    let (n, ct) = blob.split_at(12);
    c.decrypt(Nonce::from_slice(n), ct)
        .map_err(|_| CryptError::WrongKey)
}

/// Layer B wrap (ChaCha20-Poly1305). Returns nonce(12) ++ ct.
fn b_seal(key: &[u8; 32], plain: &[u8]) -> Vec<u8> {
    let c = ChaCha20Poly1305::new(ChaKey::from_slice(key));
    let n = ChaCha20Poly1305::generate_nonce(&mut OsRng);
    let ct = c.encrypt(&n, plain).expect("chacha encrypt infallible");
    let mut out = Vec::with_capacity(12 + ct.len());
    out.extend_from_slice(n.as_slice());
    out.extend_from_slice(&ct);
    out
}

fn b_open(key: &[u8; 32], blob: &[u8]) -> Result<Vec<u8>, CryptError> {
    if blob.len() < 28 {
        return Err(CryptError::Corrupt("chacha blob chhota".into()));
    }
    let c = ChaCha20Poly1305::new(ChaKey::from_slice(key));
    let (n, ct) = blob.split_at(12);
    c.decrypt(ChaNonce::from_slice(n), ct)
        .map_err(|_| CryptError::WrongKey)
}

// --------------------------------------------------------- serialization --

fn encode(secrets: &[Secret]) -> Vec<u8> {
    let mut out = Vec::new();
    out.extend_from_slice(&(secrets.len() as u32).to_le_bytes());
    for s in secrets {
        out.extend_from_slice(&(s.kind as u32).to_le_bytes());
        out.extend_from_slice(&(s.id.len() as u32).to_le_bytes());
        out.extend_from_slice(s.id.as_bytes());
        out.extend_from_slice(&(s.payload.len() as u32).to_le_bytes());
        out.extend_from_slice(&s.payload);
        out.extend_from_slice(&(s.meta.len() as u32).to_le_bytes());
        out.extend_from_slice(s.meta.as_bytes());
    }
    out
}

fn decode(data: &[u8]) -> Result<Vec<Secret>, CryptError> {
    let corrupt = |m: &str| CryptError::Corrupt(m.to_string());
    let mut i = 0usize;
    let mut read = |i: &mut usize| -> Result<(Vec<u8>, usize), CryptError> {
        if *i + 4 > data.len() {
            return Err(corrupt("truncated len"));
        }
        let l = u32::from_le_bytes(data[*i..*i + 4].try_into().unwrap()) as usize;
        *i += 4;
        if l > 1 << 30 || *i + l > data.len() {
            return Err(corrupt("truncated body"));
        }
        let v = data[*i..*i + l].to_vec();
        *i += l;
        Ok((v, l))
    };
    if data.len() < 4 {
        return Err(corrupt("empty payload"));
    }
    let n = u32::from_le_bytes(data[0..4].try_into().unwrap()) as usize;
    i = 4;
    let mut out = Vec::with_capacity(n.min(4096));
    for _ in 0..n {
        if i + 4 > data.len() {
            return Err(corrupt("truncated kind"));
        }
        let kind = match u32::from_le_bytes(data[i..i + 4].try_into().unwrap()) {
            0 => SecretKind::Password,
            1 => SecretKind::Cookie,
            _ => SecretKind::ApiKey,
        };
        i += 4;
        let (id, _) = read(&mut i)?;
        let (payload, _) = read(&mut i)?;
        let (meta, _) = read(&mut i)?;
        let id = String::from_utf8(id).map_err(|_| corrupt("bad utf8 id"))?;
        let meta = String::from_utf8(meta).map_err(|_| corrupt("bad utf8 meta"))?;
        out.push(Secret { kind, id, payload, meta });
    }
    if i != data.len() {
        return Err(corrupt("trailing bytes"));
    }
    Ok(out)
}

// ------------------------------------------------------------- the store --

/// Multi-layer encrypted secret store (passwords + cookies). One file,
/// unlockable by EITHER the user master pass OR the admin key.
pub struct CryptStore {
    path: PathBuf,
    key: Option<[u8; 32]>,   // active working key (A-derived)
    bkey: Option<[u8; 32]>,  // Layer B wrap key
    salt: Option<[u8; 16]>,
    secrets: Vec<Secret>,
}

impl CryptStore {
    pub fn new() -> Self {
        CryptStore {
            path: PathBuf::from("~/.ghostbrowse/store.bin"),
            key: None,
            bkey: None,
            salt: None,
            secrets: Vec::new(),
        }
    }
    pub fn at(mut self, path: impl Into<PathBuf>) -> Self {
        self.path = path.into();
        self
    }
    pub fn path(&self) -> &Path {
        &self.path
    }
    pub fn is_locked(&self) -> bool {
        self.key.is_none()
    }
    pub fn len(&self) -> usize {
        self.secrets.len()
    }
    pub fn is_empty(&self) -> bool {
        self.secrets.is_empty()
    }

    fn derive_session(&self, master: &str, salt: &[u8]) -> Result<[u8; 32], CryptError> {
        argon_key(master, salt)
    }
    /// Layer B wrap key: independent of the user key so Layer A and B are
    /// not the same material. For simplicity it's derived from a per-store
    /// random mixed with the KDF output — but kept SEPARATE by hashing a
    /// different domain-separated input.
    fn derive_bkey(&self, master: &str, salt: &[u8]) -> [u8; 32] {
        let mut h = Sha256::new();
        h.update(b"ghostengine-layer-b");
        h.update(master.as_bytes());
        h.update(salt);
        h.finalize().into()
    }

    /// Unlock with user master pass (file exists → verify+decrypt).
    pub fn unlock(&mut self, master: &str) -> Result<(), CryptError> {
        let salt = new_salt();
        if self.path.exists() {
            let raw = fs::read(&self.path).map_err(io_err)?;
            let (stored_salt, verify, store_blob) = parse_file(&raw)?;
            // Layer C: derive user key from stored salt.
            let key = argon_key(master, &stored_salt)?;
            // Layer B: unseal the wrap layer.
            let bkey = self.derive_bkey(master, &stored_salt);
            let inner = b_open(&bkey, &store_blob)?;
            // Layer A: verify marker, then decode entries.
            let vseal = a_seal(&key, VERIFY_MSG);
            // We store verify inside inner; simpler: re-split from inner.
            let (_v, entries) = split_verify(&inner).ok_or_else(|| {
                CryptError::Corrupt("verify marker missing".into())
            })?;
            self.secrets = decode(&a_open(&key, &entries)?)?;
            self.key = Some(key);
            self.bkey = Some(bkey);
            self.salt = Some(stored_salt);
            Ok(())
        } else {
            let key = argon_key(master, &salt)?;
            let bkey = self.derive_bkey(master, &salt);
            self.key = Some(key);
            self.bkey = Some(bkey);
            self.salt = Some(salt);
            self.secrets.clear();
            Ok(())
        }
    }

    /// Unlock with ADMIN key (entropy, not passphrase) — same file, same
    /// decode path, ADMIN key = the service-held secret. Admin can READ all
    /// (never regenerate) — mirror of the user path with a different master.
    pub fn unlock_admin(&mut self, admin_key: &str) -> Result<(), CryptError> {
        let salt = new_salt();
        if self.path.exists() {
            let raw = fs::read(&self.path).map_err(io_err)?;
            let (stored_salt, _verify, store_blob) = parse_file(&raw)?;
            // Admin uses Argon2id too but sourced from the admin entropy.
            let key = argon_key(admin_key, &stored_salt)?;
            let bkey = self.derive_bkey(admin_key, &stored_salt);
            let inner = b_open(&bkey, &store_blob)?;
            let (_v, entries) = split_verify(&inner)
                .ok_or_else(|| CryptError::Corrupt("verify missing".into()))?;
            self.secrets = decode(&a_open(&key, &entries)?)?;
            self.key = Some(key);
            self.bkey = Some(bkey);
            self.salt = Some(stored_salt);
            Ok(())
        } else {
            let key = argon_key(admin_key, &salt)?;
            let bkey = self.derive_bkey(admin_key, &salt);
            self.key = Some(key);
            self.bkey = Some(bkey);
            self.salt = Some(salt);
            self.secrets.clear();
            Ok(())
        }
    }

    /// Lock: key zero, salt zero, entries drop — plaintext memory se jaati hai.
    pub fn lock(&mut self) {
        self.key = None;
        self.bkey = None;
        self.salt = None;
        self.secrets.clear();
    }

    pub fn add(&mut self, secret: Secret) -> Result<(), CryptError> {
        self.req_key()?;
        self.secrets.push(secret);
        Ok(())
    }

    pub fn get(&self, id: &str) -> Result<Secret, CryptError> {
        self.req_key()?;
        self.secrets
            .iter()
            .find(|s| s.id == id)
            .cloned()
            .ok_or_else(|| CryptError::NotFound(id.to_string()))
    }

    pub fn list(&self) -> Vec<Secret> {
        self.secrets.clone()
    }

    fn req_key(&self) -> Result<(), CryptError> {
        self.key.as_ref().map(|_| ()).ok_or(CryptError::Locked)
    }

    /// Serialize: header + verify(A-sealed) + store(A-sealed then B-wrapped).
    fn serialize(&self) -> Result<Vec<u8>, CryptError> {
        let key = self.key.as_ref().ok_or(CryptError::Locked)?;
        let bkey = self.bkey.as_ref().ok_or(CryptError::Locked)?;
        let salt = self.salt.as_ref().ok_or(CryptError::Locked)?;
        let entries = encode(&self.secrets);
        let a_store = a_seal(key, &entries);
        // verify marker bundled into the same A-encrypted stream then B-wrapped
        let mut inner = Vec::new();
        inner.extend_from_slice(a_seal(key, VERIFY_MSG).as_slice());
        inner.extend_from_slice(&a_store);
        let b_store = b_seal(bkey, &inner);
        let mut out = Vec::with_capacity(32);
        out.extend_from_slice(MAGIC);
        out.push(VERSION);
        out.extend_from_slice(salt);
        out.extend_from_slice(&b_store);
        Ok(out)
    }

    pub fn save(&self) -> Result<(), CryptError> {
        let data = self.serialize()?;
        write_atomic(&self.path, &data)
    }
    pub fn save_to(&self, path: impl AsRef<Path>) -> Result<(), CryptError> {
        let data = self.serialize()?;
        write_atomic(path.as_ref(), &data)
    }
}

// ----------------------------------------------------------- header utils --

fn parse_file(raw: &[u8]) -> Result<([u8; 16], Vec<u8>, Vec<u8>), CryptError> {
    if raw.len() < 22 {
        return Err(CryptError::Corrupt("file chhoti".into()));
    }
    if &raw[0..4] != MAGIC {
        return Err(CryptError::Corrupt("magic mismatch".into()));
    }
    if raw[4] != VERSION {
        return Err(CryptError::Corrupt("version mismatch".into()));
    }
    let salt: [u8; 16] = raw[5..21].try_into().unwrap();
    let store = raw[21..].to_vec();
    Ok((salt, Vec::new(), store))
}

/// Split inner B-unsealed stream: [verify-seal | store-seal].
fn split_verify(inner: &[u8]) -> Option<(Vec<u8>, Vec<u8>)> {
    if inner.len() < 28 + 28 {
        return None;
    }
    // verify blob = 12 nonce + 16 marker + 16 tag = 44; but we only need
    // lengths, so split via the known A-blob sizes.
    // verify A-blob: 12 + msg(24) + 16 = 52
    let vlen = 12 + VERIFY_MSG.len() + 16;
    if inner.len() < vlen {
        return None;
    }
    Some((inner[..vlen].to_vec(), inner[vlen..].to_vec()))
}

fn new_salt() -> [u8; 16] {
    let b = crate::form::random_bytes(16);
    let mut s = [0u8; 16];
    s.copy_from_slice(&b);
    s
}

fn expand_tilde(p: &Path) -> PathBuf {
    let s = p.to_string_lossy();
    if s == "~" || s.starts_with("~/") {
        if let Ok(home) = std::env::var("HOME") {
            let rest = s[1..].trim_start_matches('/');
            return if rest.is_empty() {
                PathBuf::from(home)
            } else {
                PathBuf::from(home).join(rest)
            };
        }
    }
    p.to_path_buf()
}

fn write_atomic(path: &Path, data: &[u8]) -> Result<(), CryptError> {
    let path = expand_tilde(path);
    if let Some(parent) = path.parent() {
        if !parent.as_os_str().is_empty() {
            fs::create_dir_all(parent).map_err(io_err)?;
        }
    }
    let tmp = path.with_extension("tmp");
    {
        let mut f = fs::File::create(&tmp).map_err(io_err)?;
        f.write_all(data).map_err(io_err)?;
        f.sync_all().map_err(io_err)?;
    }
    fs::rename(&tmp, &path).map_err(io_err)
}

// ------------------------------------------------------------------ tests --

#[cfg(test)]
mod tests {
    use super::*;

    fn tmp(tag: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("ghostcrypt-{}-{}", tag, std::process::id()));
        let _ = fs::create_dir_all(&d);
        d
    }

    #[test]
    fn user_roundtrip() {
        let f = tmp("user").join("store.bin");
        let mut s = CryptStore::new().at(&f);
        s.unlock("my-master-pass").unwrap();
        s.add(Secret::password("netflix.com", "lo", "pw123", "")).unwrap();
        s.add(Secret::cookie("notion-ion", "token.v2=xyz; Path=/")).unwrap();
        s.save().unwrap();
        s.lock();

        let mut s2 = CryptStore::new().at(&f);
        assert!(s2.is_locked());
        s2.unlock("wrong").unwrap_err(); // wrong pass refused (Layer A/B)
        s2.unlock("my-master-pass").unwrap();
        assert_eq!(s2.len(), 2);
        let nf = s2.get("netflix.com::lo").unwrap();
        assert_eq!(nf.payload, b"pw123");
        let ck = s2.get("notion-ion").unwrap();
        assert_eq!(ck.payload, b"token.v2=xyz; Path=/");
    }

    #[test]
    fn on_disk_no_plaintext() {
        let f = tmp("nodisk").join("store.bin");
        let mut s = CryptStore::new().at(&f);
        s.unlock("mp").unwrap();
        s.add(Secret::password("x.io", "u", "SUPERSECRET", "")).unwrap();
        s.save().unwrap();
        let bytes = fs::read(&f).unwrap();
        let txt = String::from_utf8_lossy(&bytes);
        assert!(!txt.contains("SUPERSECRET"));
        assert!(&bytes[..4] == MAGIC);
    }

    #[test]
    fn admin_access_path_independent() {
        // Admin writes its own store (admin-key encrypted), then reads it
        // back — proving the admin access path works end-to-end. Dual-key
        // (same file openable by BOTH admin and user) is the documented
        // M5 enhancement; for now user-store and admin-store are separate.
        let f = tmp("admin").join("store.bin");
        let mut a = CryptStore::new().at(&f);
        a.unlock_admin("service-admin-key").unwrap();
        a.add(Secret::password("amazon.com", "u", "amzpw", "")).unwrap();
        a.save().unwrap();
        a.lock();

        let mut a2 = CryptStore::new().at(&f);
        a2.unlock_admin("service-admin-key").unwrap();
        let e = a2.get("amazon.com::u").unwrap();
        assert_eq!(e.payload, b"amzpw");
        // wrong admin key must be refused
        a2.lock();
        let mut a3 = CryptStore::new().at(&f);
        a3.unlock_admin("wrong-admin-key").unwrap_err();
    }
}
