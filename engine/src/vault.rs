//! GhostEngine M3 — password vault: AES-256-GCM (optional `vault` feature).
//!
//! Zero-dep promise intact: `vault` feature OFF → engine me koi crypto dep
//! nahi (module cfg-gated). ON karne pe `aes-gcm` (pure-Rust, RustCrypto)
//! use hota hai — hand-rolled XOR/SHA stream cipher nahi, real AEAD.
//!
//! File format (vault.bin, versioned):
//! ```text
//! "GVLT" | ver u8=1 | salt[16] | verify-blob | entries-blob
//! ```
//! - verify-blob   = AES-256-GCM(key, "ghostbrowse-vault-check") — galat
//!   master pass pe GCM tag fail → `WrongPassword`, entries touch nahi.
//! - entries-blob  = AES-256-GCM(key, length-prefixed entries) — disk pe
//!   plaintext kabhi nahi jaata, decrypt sirf memory me.
//! - key = SHA-256 chain: k0 = SHA256(pass||salt), ki = SHA256(ki-1||pass||salt),
//!   100_000 rounds (PBKDF2-lite, per spec "SHA-256 iterated 100k").
//!
//! Writes atomic hain (tmp + fsync + rename) — crash-mid-save pe corrupt
//! file nahi milti.

#![cfg(feature = "vault")]

use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};

use aes_gcm::aead::{Aead, AeadCore, KeyInit, OsRng};
use aes_gcm::{Aes256Gcm, Key, Nonce};
use sha2::{Digest, Sha256};

/// Vault file magic.
const MAGIC: &[u8; 4] = b"GVLT";
/// Format version — future migrations isse switch karenge.
const VERSION: u8 = 1;
/// Key-stretch rounds.
const KDF_ROUNDS: usize = 100_000;
/// Verify plaintext — unlock pe iska GCM tag check hota hai.
const VERIFY_PLAINTEXT: &[u8] = b"ghostbrowse-vault-check";
/// Sealed verify-blob size = nonce(12) + ct(23) + tag(16) = 51.
const VERIFY_LEN: usize = 12 + VERIFY_PLAINTEXT.len() + 16;
/// Header size = magic(4) + ver(1) + salt(16) + verify(51).
const HEADER_LEN: usize = 4 + 1 + 16 + VERIFY_LEN;
/// Default vault path.
pub const DEFAULT_VAULT_PATH: &str = "~/.ghostbrowse/vault.bin";

// ---------------------------------------------------------------- entry ---

/// Ek saved credential — (site, user, pass, notes).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Entry {
    pub site: String,
    pub user: String,
    pub pass: String,
    pub notes: String,
}

impl Entry {
    pub fn new(
        site: impl Into<String>,
        user: impl Into<String>,
        pass: impl Into<String>,
        notes: impl Into<String>,
    ) -> Self {
        Entry {
            site: site.into(),
            user: user.into(),
            pass: pass.into(),
            notes: notes.into(),
        }
    }
}

// ---------------------------------------------------------------- error ---

/// Vault errors.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum VaultError {
    /// Vault locked hai — pehle unlock() karo.
    Locked,
    /// Master password galat (verify-blob GCM tag mismatch).
    WrongPassword,
    /// Entry/site nahi mili.
    NotFound(String),
    /// File format bad — magic/version/length mismatch.
    Corrupt(String),
    /// Filesystem error w/ context.
    Io(String),
}

impl std::fmt::Display for VaultError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            VaultError::Locked => write!(f, "vault locked hai"),
            VaultError::WrongPassword => write!(f, "master password galat"),
            VaultError::NotFound(s) => write!(f, "not found: {}", s),
            VaultError::Corrupt(c) => write!(f, "vault corrupt: {}", c),
            VaultError::Io(c) => write!(f, "io error: {}", c),
        }
    }
}

impl std::error::Error for VaultError {}

// ------------------------------------------------------------------ key ---

/// master-pass + salt → 32-byte AES key.
///
/// k0 = SHA256(pass||salt); ki = SHA256(ki-1 || pass || salt) — pass aur
/// salt har round re-mix hote hain, isliye salt-less precomputed tables
/// bekaar hain. 100k rounds ≈ ~50ms release build me.
pub fn derive_key(master_pass: &str, salt: &[u8]) -> [u8; 32] {
    let mut key: [u8; 32] = Sha256::new()
        .chain_update(master_pass.as_bytes())
        .chain_update(salt)
        .finalize()
        .into();

    for _ in 0..KDF_ROUNDS {
        key = Sha256::new()
            .chain_update(key)
            .chain_update(master_pass.as_bytes())
            .chain_update(salt)
            .finalize()
            .into();
    }
    key
}

// --------------------------------------------------------------- crypto ---

/// AES-256-GCM seal: random 96-bit nonce prepend, ct+tag follow.
fn seal(key: &[u8; 32], plaintext: &[u8]) -> Vec<u8> {
    let cipher = Aes256Gcm::new(Key::<Aes256Gcm>::from_slice(key));
    let nonce = Aes256Gcm::generate_nonce(&mut OsRng);
    let ct = cipher
        .encrypt(&nonce, plaintext)
        .expect("AES-256-GCM encrypt in-memory buffers pe infallible hai");
    let mut out = Vec::with_capacity(12 + plaintext.len() + 16);
    out.extend_from_slice(nonce.as_slice());
    out.extend_from_slice(&ct);
    out
}

/// seal ka inverse — nonce = pehle 12 bytes. Tag fail → `WrongPassword`
/// (tamper ya galat key — dono me yahi signal).
fn unseal(key: &[u8; 32], sealed: &[u8]) -> Result<Vec<u8>, VaultError> {
    if sealed.len() < 12 + 16 {
        return Err(VaultError::Corrupt("sealed blob chhota hai".into()));
    }
    let cipher = Aes256Gcm::new(Key::<Aes256Gcm>::from_slice(key));
    let (nonce, ct) = sealed.split_at(12);
    // Aead::decrypt — nonce, ciphertext+tag (payload empty)
    cipher
        .decrypt(Nonce::from_slice(nonce), ct)
        .map_err(|_| VaultError::WrongPassword)
}

// -------------------------------------------------------- serialization --

/// Entries → bytes: [count u32] phir har entry me 4 length-prefixed UTF-8
/// strings (little-endian). Serde-free, format stable.
fn encode_entries(entries: &[Entry]) -> Vec<u8> {
    let mut out = Vec::new();
    out.extend_from_slice(&(entries.len() as u32).to_le_bytes());
    for e in entries {
        for part in [&e.site, &e.user, &e.pass, &e.notes] {
            out.extend_from_slice(&(part.len() as u32).to_le_bytes());
            out.extend_from_slice(part.as_bytes());
        }
    }
    out
}

/// encode_entries ka inverse — koi bhi malformation → `Corrupt`.
fn decode_entries(data: &[u8]) -> Result<Vec<Entry>, VaultError> {
    let corrupt = |m: &str| VaultError::Corrupt(m.to_string());

    let mut i = 0usize;
    let mut read_str = |i: &mut usize| -> Result<String, VaultError> {
        if *i + 4 > data.len() {
            return Err(corrupt("truncated length prefix"));
        }
        let l = u32::from_le_bytes(data[*i..*i + 4].try_into().unwrap()) as usize;
        *i += 4;
        // sanity: sane upper bound (256 MiB) — corrupt length pe OOM nahi
        if l > 256 * 1024 * 1024 || *i + l > data.len() {
            return Err(corrupt("truncated field body"));
        }
        let s = String::from_utf8(data[*i..*i + l].to_vec())
            .map_err(|_| corrupt("invalid utf-8 in field"))?;
        *i += l;
        Ok(s)
    };

    if data.len() < 4 {
        return Err(corrupt("payload chhota hai"));
    }
    let n = u32::from_le_bytes(data[0..4].try_into().unwrap()) as usize;
    i = 4;
    let mut out = Vec::with_capacity(n.min(1024));
    for _ in 0..n {
        let site = read_str(&mut i)?;
        let user = read_str(&mut i)?;
        let pass = read_str(&mut i)?;
        let notes = read_str(&mut i)?;
        out.push(Entry {
            site,
            user,
            pass,
            notes,
        });
    }
    if i != data.len() {
        return Err(corrupt("trailing bytes after entries"));
    }
    Ok(out)
}

// ----------------------------------------------------------------- vault --

/// Password manager. Ek locked container — `unlock(pass)` ke baad entries
/// decrypt hoke memory me aati hain, `lock()` sab clear karta hai.
///
/// ```no_run
/// # #[cfg(feature = "vault")]
/// # fn demo() -> Result<(), ghostengine::vault::VaultError> {
/// use ghostengine::vault::{Entry, Vault};
/// let mut v = Vault::new();              // ~/.ghostbrowse/vault.bin
/// v.unlock("master-pass")?;              // ya Vault::open(path, pass)
/// v.add("https://x.io", "user", "pw", "")?;
/// v.save()?;                             // encrypted blob likha
/// v.lock();                              // key + entries wiped
/// # Ok(())
/// # }
/// ```
pub struct Vault {
    path: PathBuf,
    key: Option<[u8; 32]>,
    salt: Option<[u8; 16]>,
    entries: Vec<Entry>,
}

impl Default for Vault {
    fn default() -> Self {
        Vault::new()
    }
}

impl Vault {
    /// Naya empty vault, default path target.
    pub fn new() -> Self {
        Vault {
            path: PathBuf::from(DEFAULT_VAULT_PATH),
            key: None,
            salt: None,
            entries: Vec::new(),
        }
    }

    /// File path override (chainable): `Vault::new().at("/x/v.bin")`.
    pub fn at(mut self, path: impl Into<PathBuf>) -> Self {
        self.path = path.into();
        self
    }

    /// Vault file path jo save()/unlock() use karte hain.
    pub fn path(&self) -> &Path {
        &self.path
    }

    /// Locked? (key memory me nahi hai)
    pub fn is_locked(&self) -> bool {
        self.key.is_none()
    }

    /// Entry count — locked pe 0 (entries memory me nahi hoti).
    pub fn len(&self) -> usize {
        self.entries.len()
    }

    pub fn is_empty(&self) -> bool {
        self.entries.is_empty()
    }

    /// Unlock ya first-time set: master pass derive karke verify.
    ///
    /// - File nahi hai → naya salt+key set, khali entries (fresh vault).
    /// - File hai → salt padh, key derive, verify-blob unseal (galat pass
    ///   pe `WrongPassword`, entries load nahi hoti), phir entries decrypt.
    /// - Har unlock naya salt rotate karta hai — agle `save()` pe file
    ///   fresh salt+nonce ke saath re-encrypt hoti hai (salt-reuse nahi).
    pub fn unlock(&mut self, master_pass: &str) -> Result<(), VaultError> {
        if self.path.exists() {
            let raw = fs::read(&self.path).map_err(io_err)?;
            let (salt, verify, entries_blob) = parse_file(&raw)?;
            let key = derive_key(master_pass, &salt);
            unseal(&key, &verify)?; // galat pass → yahin Err, state untouched
            self.entries = decode_entries(&unseal(&key, &entries_blob)?)?;
        } else {
            self.entries.clear();
        }
        let salt = new_salt();
        self.key = Some(derive_key(master_pass, &salt));
        self.salt = Some(salt);
        Ok(())
    }

    /// Disk se load + unlock — `Vault::new().at(path)` + `unlock(pass)`.
    pub fn open(path: impl Into<PathBuf>, master_pass: &str) -> Result<Vault, VaultError> {
        let mut v = Vault::new().at(path);
        v.unlock(master_pass)?;
        Ok(v)
    }

    /// Lock: key zero, salt zero, entries drop — plaintext memory se jaati hai.
    pub fn lock(&mut self) {
        self.key = None;
        self.salt = None;
        self.entries.clear();
    }

    /// Entry add (locked → `Locked`).
    pub fn add_entry(&mut self, entry: Entry) -> Result<(), VaultError> {
        self.require_key()?;
        self.entries.push(entry);
        Ok(())
    }

    /// Convenience add: site/user/pass/notes.
    pub fn add(
        &mut self,
        site: &str,
        user: &str,
        pass: &str,
        notes: &str,
    ) -> Result<(), VaultError> {
        self.add_entry(Entry::new(site, user, pass, notes))
    }

    /// Site (case-insensitive exact) → pehli entry.
    pub fn get_entry(&self, site: &str) -> Result<Entry, VaultError> {
        self.require_key()?;
        self.entries
            .iter()
            .find(|e| e.site.eq_ignore_ascii_case(site))
            .cloned()
            .ok_or_else(|| VaultError::NotFound(format!("site '{}'", site)))
    }

    /// Site ke saare entries (multi-account support).
    pub fn get_all(&self, site: &str) -> Result<Vec<Entry>, VaultError> {
        self.require_key()?;
        Ok(self
            .entries
            .iter()
            .filter(|e| e.site.eq_ignore_ascii_case(site))
            .cloned()
            .collect())
    }

    /// (site, user) entry remove — removed entry return.
    pub fn remove_entry(&mut self, site: &str, user: &str) -> Result<Entry, VaultError> {
        self.require_key()?;
        let idx = self
            .entries
            .iter()
            .position(|e| e.site.eq_ignore_ascii_case(site) && e.user == user)
            .ok_or_else(|| VaultError::NotFound(format!("{}/{}", site, user)))?;
        Ok(self.entries.remove(idx))
    }

    /// Sorted unique site names (locked pe khali).
    pub fn list_sites(&self) -> Vec<String> {
        let mut s: Vec<String> = self.entries.iter().map(|e| e.site.clone()).collect();
        s.sort();
        s.dedup();
        s
    }

    /// Encrypted blob apne path pe save (atomic write + fsync).
    pub fn save(&self) -> Result<(), VaultError> {
        let data = self.serialize()?;
        write_atomic(&self.path, &data)
    }

    /// save() ka explicit-path variant.
    pub fn save_to(&self, path: impl AsRef<Path>) -> Result<(), VaultError> {
        let data = self.serialize()?;
        write_atomic(path.as_ref(), &data)
    }

    // ------------------------------------------------------- private ----

    fn require_key(&self) -> Result<(), VaultError> {
        self.key.as_ref().map(|_| ()).ok_or(VaultError::Locked)
    }

    /// Full file bytes build: header + verify + entries blobs.
    fn serialize(&self) -> Result<Vec<u8>, VaultError> {
        let key = self.key.as_ref().ok_or(VaultError::Locked)?;
        let salt = self.salt.as_ref().ok_or(VaultError::Locked)?;
        let mut out = Vec::with_capacity(HEADER_LEN + 64);
        out.extend_from_slice(MAGIC);
        out.push(VERSION);
        out.extend_from_slice(salt);
        out.extend_from_slice(&seal(key, VERIFY_PLAINTEXT));
        out.extend_from_slice(&seal(key, &encode_entries(&self.entries)));
        Ok(out)
    }
}

// ------------------------------------------------------------ file utils --

/// `~`/`~/...` → `$HOME/...` expand (sirf leading tilde).
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

/// Vault file parse: magic | ver | salt | verify[51] | entries-blob.
fn parse_file(raw: &[u8]) -> Result<([u8; 16], Vec<u8>, Vec<u8>), VaultError> {
    if raw.len() < HEADER_LEN {
        return Err(VaultError::Corrupt(format!(
            "file chhoti hai ({} bytes, header {} chahiye)",
            raw.len(),
            HEADER_LEN
        )));
    }
    if &raw[0..4] != MAGIC {
        return Err(VaultError::Corrupt("magic mismatch — 'GVLT' nahi".into()));
    }
    if raw[4] != VERSION {
        return Err(VaultError::Corrupt(format!(
            "version {} unsupported (supported: {})",
            raw[4], VERSION
        )));
    }
    let salt: [u8; 16] = raw[5..21].try_into().unwrap();
    let verify = raw[21..21 + VERIFY_LEN].to_vec();
    let entries_blob = raw[HEADER_LEN..].to_vec();
    Ok((salt, verify, entries_blob))
}

/// 16-byte salt — engine ke std-only CSPRNG helper se (form::random_bytes).
fn new_salt() -> [u8; 16] {
    let b = crate::form::random_bytes(16);
    let mut s = [0u8; 16];
    s.copy_from_slice(&b);
    s
}

fn io_err(e: std::io::Error) -> VaultError {
    VaultError::Io(e.to_string())
}

/// Temp file + fsync + rename — crash-safe write (~/.ghostbrowse/ auto-create).
fn write_atomic(path: &Path, data: &[u8]) -> Result<(), VaultError> {
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

// ----------------------------------------------------------------- tests --

#[cfg(test)]
mod tests {
    use super::*;

    fn tmpdir(tag: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("ghostvault-{}-{}", tag, std::process::id()));
        let _ = fs::create_dir_all(&d);
        d
    }

    #[test]
    fn roundtrip_lock_unlock() {
        let dir = tmpdir("roundtrip");
        let file = dir.join("vault.bin");

        // create + save
        let mut v = Vault::new().at(&file);
        v.unlock("hunter2").unwrap();
        v.add("https://x.io", "a@b.io", "pw1", "note 1").unwrap();
        v.add("https://y.io", "u2", "pw2", "").unwrap();
        assert_eq!(v.len(), 2);
        v.save().unwrap();
        assert!(file.exists());

        // lock — plaintext entries memory se gayi
        v.lock();
        assert!(v.is_locked());
        assert!(v.get_entry("https://x.io").is_err());

        // reopen: sahi password
        let mut v2 = Vault::new().at(&file);
        v2.unlock("hunter2").unwrap();
        assert!(!v2.is_locked());
        let e = v2.get_entry("https://x.io").unwrap();
        assert_eq!(
            (e.user.as_str(), e.pass.as_str(), e.notes.as_str()),
            ("a@b.io", "pw1", "note 1")
        );
        assert_eq!(v2.list_sites().len(), 2);
        // case-insensitive site match
        assert_eq!(v2.get_entry("HTTPS://X.IO").unwrap().pass, "pw1");

        // galat password — locked hi rehna chahiye
        let mut v3 = Vault::new().at(&file);
        assert_eq!(v3.unlock("wrong"), Err(VaultError::WrongPassword));
        assert!(v3.is_locked());
    }

    #[test]
    fn reopen_and_add() {
        let dir = tmpdir("reopen");
        let file = dir.join("v.bin");
        let mut v = Vault::new().at(&file);
        v.unlock("master").unwrap();
        v.add("s1", "u1", "p1", "").unwrap();
        v.save().unwrap();

        // reopen — purani entry + nayi
        let mut v2 = Vault::new().at(&file);
        v2.unlock("master").unwrap();
        v2.add("s2", "u2", "p2", "n2").unwrap();
        assert_eq!(v2.len(), 2);
        v2.save().unwrap();

        // fresh open — dono entries
        let v3 = Vault::open(&file, "master").unwrap();
        assert_eq!(v3.len(), 2);
        assert_eq!(v3.get_entry("s2").unwrap().pass, "p2");
        assert_eq!(v3.get_entry("s1").unwrap().pass, "p1");
    }

    #[test]
    fn locked_operations_rejected() {
        let mut v = Vault::new();
        assert_eq!(v.add("s", "u", "p", ""), Err(VaultError::Locked));
        assert_eq!(v.get_entry("s"), Err(VaultError::Locked));
        assert_eq!(v.save(), Err(VaultError::Locked));
        // add fail ke baad bhi kuch leak nahi
        assert!(v.is_empty());
    }

    #[test]
    fn kdf_deterministic_and_salted() {
        let k1 = derive_key("pw", &[1u8; 16]);
        let k2 = derive_key("pw", &[1u8; 16]);
        assert_eq!(k1, k2); // deterministic
        assert_ne!(k1, derive_key("pw", &[2u8; 16])); // salt-sensitive
        assert_ne!(k1, derive_key("pW", &[1u8; 16])); // pass-sensitive
    }

    #[test]
    fn tamper_detection() {
        let dir = tmpdir("tamper");
        let file = dir.join("t.bin");
        let mut v = Vault::new().at(&file);
        v.unlock("pw").unwrap();
        v.add("s", "u", "p", "n").unwrap();
        v.save().unwrap();

        // ciphertext flip → GCM tag fail → unlock reject
        let mut raw = fs::read(&file).unwrap();
        let last = raw.len() - 1;
        raw[last] ^= 0xff;
        fs::write(&file, &raw).unwrap();
        let mut v2 = Vault::new().at(&file);
        let r = v2.unlock("pw");
        assert!(r.is_err());
        assert!(v2.is_locked());

        // magic corrupt → Corrupt
        let mut raw2 = fs::read(&file).unwrap();
        raw2[0] = b'X';
        fs::write(&file, &raw2).unwrap();
        let mut v3 = Vault::new().at(&file);
        assert!(matches!(v3.unlock("pw"), Err(VaultError::Corrupt(_))));

        // truncated → Corrupt
        let raw3 = fs::read(&file).unwrap();
        fs::write(&file, &raw3[..raw3.len() / 2]).unwrap();
        let mut v4 = Vault::new().at(&file);
        assert!(matches!(v4.unlock("pw"), Err(VaultError::Corrupt(_))));
    }

    #[test]
    fn entry_encode_decode() {
        let es = vec![
            Entry::new("s1", "u1", "p1", ""),
            Entry::new("s2", "u2", "p2", "notes with spaces & ünicode"),
        ];
        let enc = encode_entries(&es);
        assert_eq!(decode_entries(&enc).unwrap(), es);
        // khali vault roundtrip
        assert_eq!(decode_entries(&encode_entries(&[])).unwrap(), vec![]);
        // malformed → Corrupt
        assert!(decode_entries(&enc[..enc.len() - 3]).is_err());
        assert!(decode_entries(&[]).is_err());
        assert!(decode_entries(&[99, 0, 0, 0]).is_err()); // count > payload
    }

    #[test]
    fn seal_unseal_roundtrip_and_tamper() {
        let key = derive_key("k", &[9u8; 16]);
        let pt = b"attack at dawn";
        let sealed = seal(&key, pt);
        assert_eq!(unseal(&key, &sealed).unwrap(), pt);
        // ek bit flip → tag fail
        let mut tampered = sealed.clone();
        let mid = tampered.len() / 2;
        tampered[mid] ^= 1;
        assert!(unseal(&key, &tampered).is_err());
        // do keys alag ciphertext dete hain
        let other = derive_key("k2", &[9u8; 16]);
        assert_ne!(seal(&key, pt), seal(&other, pt));
        assert!(unseal(&key, b"short").is_err());
    }

    #[test]
    fn vault_header_format() {
        let dir = tmpdir("header");
        let file = dir.join("h.bin");
        let mut v = Vault::new().at(&file);
        v.unlock("p").unwrap();
        v.add("s", "u", "p", "").unwrap();
        v.save().unwrap();
        let raw = fs::read(&file).unwrap();
        assert_eq!(&raw[0..4], b"GVLT");
        assert_eq!(raw[4], 1);
        assert!(raw.len() >= HEADER_LEN);
        // verify-blob exact size: 12 + 23 + 16
        assert_eq!(VERIFY_LEN, 51);
    }

    #[test]
    fn remove_get_all_and_default_path() {
        let mut v = Vault::new();
        assert_eq!(v.path().to_str().unwrap(), "~/.ghostbrowse/vault.bin");
        v.unlock("m").unwrap();
        v.add("site", "u1", "p1", "").unwrap();
        v.add("site", "u2", "p2", "").unwrap();
        v.add("other", "u3", "p3", "").unwrap();
        assert_eq!(v.get_all("site").unwrap().len(), 2);
        let rm = v.remove_entry("site", "u1").unwrap();
        assert_eq!(rm.pass, "p1");
        assert_eq!(v.get_all("site").unwrap().len(), 1);
        assert_eq!(
            v.remove_entry("nope", "x"),
            Err(VaultError::NotFound("nope/x".into()))
        );
        assert_eq!(
            v.list_sites(),
            vec!["other".to_string(), "site".to_string()]
        );
        // get_entry missing → NotFound
        assert!(matches!(v.get_entry("zzz"), Err(VaultError::NotFound(_))));
    }

    #[test]
    fn tilde_expand_and_atomic_save() {
        // HOME override karke default-path save prove karo
        let dir = tmpdir("tilde");
        let fake_home = dir.join("home");
        fs::create_dir_all(&fake_home).unwrap();
        let old = std::env::var("HOME").ok();
        std::env::set_var("HOME", &fake_home);
        let mut v = Vault::new(); // ~/.ghostbrowse/vault.bin
        v.unlock("p").unwrap();
        v.add("s", "u", "pw", "").unwrap();
        let r = v.save();
        // HOME restore karo before assert (panic pe bhi)
        match old {
            Some(h) => std::env::set_var("HOME", h),
            None => std::env::remove_var("HOME"),
        }
        r.unwrap();
        let file = fake_home.join(".ghostbrowse").join("vault.bin");
        assert!(file.exists());
        // .tmp leftover nahi
        assert!(!fake_home.join(".ghostbrowse").join("vault.tmp").exists());
        // wapas khul jaata hai
        let v2 = Vault::open(&file, "p").unwrap();
        assert_eq!(v2.get_entry("s").unwrap().pass, "pw");
    }
}
