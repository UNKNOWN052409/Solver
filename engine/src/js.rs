//! js.rs — GhostEngine ka mini-JS interpreter, scratch se, std-only.
//!
//! Honest known-gaps (ye FULL JS nahi hai):
//! - No: prototypes/classes/this-binding/async/await/regex/generators/
//!   destructuring/spread/modules/let-scoping differences (var jaisa)
//! - Strings immutable slices only; no Unicode grapheme care
//! - Numbers f64 only (no BigInt); for-of nahi (for..in bhi nahi)
//! - try/catch/throw basic (finally nahi)
//! Coverage: essential scripting — var/func/closures/obj/arr/JSON/
//!   string+array methods/Math — fetch-style sites ke liye kaafi.

use std::collections::BTreeMap;
use std::fmt::Write as _;

// ======================================================== Values --
#[derive(Debug, Clone)]
pub enum Value {
    Undefined,
    Null,
    Bool(bool),
    Num(f64),
    Str(String),
    Arr(Vec<Value>),
    Obj(BTreeMap<String, Value>),
    Func(RcFunc),
}

#[derive(Debug, Clone)]
pub struct RcFunc {
    pub name: String,
    pub params: Vec<String>,
    pub body: Vec<Stmt>,
    pub env: std::rc::Rc<RefEnv>,
}

impl Value {
    pub fn truthy(&self) -> bool {
        match self {
            Value::Undefined | Value::Null => false,
            Value::Bool(b) => *b,
            Value::Num(n) => *n != 0.0 && !n.is_nan(),
            Value::Str(s) => !s.is_empty(),
            Value::Arr(a) => !a.is_empty(),
            Value::Obj(_) => true,
            Value::Func(_) => true,
        }
    }
    pub fn type_name(&self) -> &'static str {
        match self {
            Value::Undefined => "undefined",
            Value::Null => "null",
            Value::Bool(_) => "boolean",
            Value::Num(_) => "number",
            Value::Str(_) => "string",
            Value::Arr(_) => "array",
            Value::Obj(_) => "object",
            Value::Func(_) => "function",
        }
    }
    /// JS-style loose equality (== core; === bhi yahi since types fixed).
    pub fn eq_loose(&self, o: &Value) -> bool {
        match (self, o) {
            (Value::Undefined, Value::Undefined) | (Value::Null, Value::Null) => true,
            (Value::Null, Value::Undefined) | (Value::Undefined, Value::Null) => true,
            (Value::Bool(a), Value::Bool(b)) => a == b,
            (Value::Num(a), Value::Num(b)) => a == b,
            (Value::Str(a), Value::Str(b)) => a == b,
            (Value::Bool(a), Value::Num(b)) => (*a as i64 as f64) == *b,
            (Value::Num(a), Value::Bool(b)) => *a == (*b as i64 as f64),
            (Value::Arr(a), Value::Arr(b)) => std::ptr::eq(
                a as *const Vec<Value> as *const u8,
                b as *const Vec<Value> as *const u8,
            ),
            _ => false,
        }
    }
}


impl PartialEq for Value {
    fn eq(&self, other: &Self) -> bool {
        use Value::*;
        match (self, other) {
            (Undefined, Undefined) | (Null, Null) => true,
            (Bool(a), Bool(b)) => a == b,
            (Num(a), Num(b)) => a == b,
            (Str(a), Str(b)) => a == b,
            (Arr(a), Arr(b)) => a == b,
            (Obj(a), Obj(b)) => a == b,
            _ => false,
        }
    }
}

impl std::fmt::Display for Value {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Value::Undefined => write!(f, "undefined"),
            Value::Null => write!(f, "null"),
            Value::Bool(b) => write!(f, "{}", b),
            Value::Num(n) => {
                if n.fract() == 0.0 && n.abs() < 1e15 {
                    write!(f, "{}", *n as i64)
                } else {
                    write!(f, "{}", n)
                }
            }
            Value::Str(s) => write!(f, "{}", s),
            Value::Arr(a) => {
                let parts: Vec<String> = a.iter().map(|v| v.to_string()).collect();
                write!(f, "[{}]", parts.join(","))
            }
            Value::Obj(_) => write!(f, "[object Object]"),
            Value::Func(fun) => write!(f, "function {}(){{...}}", fun.name),
        }
    }
}

// ======================================================== Lexer --
#[derive(Debug, Clone, PartialEq)]
pub enum Tok {
    Num(f64),
    Str(String),
    Ident(String),
    Punct(String), // + - * / % = == === != !== < > <= >= && || ! ( ) { } [ ] , ; . ? :
    Kw(&'static str),
    Eof,
}

pub fn lex(src: &str) -> Result<Vec<Tok>, String> {
    let b: Vec<char> = src.chars().collect();
    let mut i = 0usize;
    let mut out = Vec::new();
    let n = b.len();
    while i < n {
        let c = b[i];
        if c.is_whitespace() {
            i += 1;
            continue;
        }
        if c == '/' && i + 1 < n && b[i + 1] == '/' {
            while i < n && b[i] != '\n' {
                i += 1;
            }
            continue;
        }
        if c == '/' && i + 1 < n && b[i + 1] == '*' {
            i += 2;
            while i + 1 < n && !(b[i] == '*' && b[i + 1] == '/') {
                i += 1;
            }
            i = (i + 2).min(n);
            continue;
        }
        // number
        if c.is_ascii_digit() {
            let s = i;
            while i < n && (b[i].is_ascii_digit() || b[i] == '.') {
                i += 1;
            }
            let txt: String = b[s..i].iter().collect();
            let num: f64 = txt.parse().map_err(|_| format!("bad number '{}'", txt))?;
            out.push(Tok::Num(num));
            continue;
        }
        // string
        if c == '"' || c == '\'' {
            let q = c;
            i += 1;
            let mut s = String::new();
            while i < n && b[i] != q {
                if b[i] == '\\' && i + 1 < n {
                    i += 1;
                    match b[i] {
                        'n' => s.push('\n'),
                        't' => s.push('\t'),
                        '\\' => s.push('\\'),
                        '"' => s.push('"'),
                        '\'' => s.push('\''),
                        '/' => s.push('/'),
                        other => s.push(other),
                    }
                } else {
                    s.push(b[i]);
                }
                i += 1;
            }
            if i >= n {
                return Err("unterminated string".into());
            }
            i += 1; // closing quote
            out.push(Tok::Str(s));
            continue;
        }
        // ident/keyword
        if c.is_ascii_alphabetic() || c == '_' || c == '$' {
            let s = i;
            while i < n && (b[i].is_ascii_alphanumeric() || b[i] == '_' || b[i] == '$') {
                i += 1;
            }
            let txt: String = b[s..i].iter().collect();
            let t = match txt.as_str() {
                "var" | "let" | "const" | "function" | "if" | "else" | "while" | "for"
                | "return" | "true" | "false" | "null" | "try" | "catch" | "throw"
                | "typeof" | "new" => Tok::Kw(match txt.as_str() {
                    "var" => "var",
                    "let" => "let",
                    "const" => "const",
                    "function" => "function",
                    "if" => "if",
                    "else" => "else",
                    "while" => "while",
                    "for" => "for",
                    "return" => "return",
                    "true" => "true",
                    "false" => "false",
                    "null" => "null",
                    "try" => "try",
                    "catch" => "catch",
                    "throw" => "throw",
                    "typeof" => "typeof",
                    _ => "new",
                }),
                _ => Tok::Ident(txt),
            };
            out.push(t);
            continue;
        }
        // punct (multi-char pehle)
        let two: String = b[i..(i + 2).min(n)].iter().collect();
        let three: String = b[i..(i + 3).min(n)].iter().collect();
        if three == "===" || three == "!==" {
            out.push(Tok::Punct(three));
            i += 3;
            continue;
        }
        if two == "==" || two == "!=" || two == "<=" || two == ">=" || two == "&&" || two == "||" {
            out.push(Tok::Punct(two));
            i += 2;
            continue;
        }
        if "+-*/%=<>!(){}[],;.?:".contains(c) {
            out.push(Tok::Punct(c.to_string()));
            i += 1;
            continue;
        }
        return Err(format!("unexpected char '{}'", c));
    }
    out.push(Tok::Eof);
    Ok(out)
}

// ======================================================== AST --
#[derive(Debug, Clone)]
pub enum Expr {
    Num(f64),
    Str(String),
    Bool(bool),
    Null,
    Undefined,
    Ident(String),
    Arr(Vec<Expr>),
    Obj(Vec<(String, Expr)>),
    Bin(String, Box<Expr>, Box<Expr>),
    Un(String, Box<Expr>),
    Assign(String, Box<Expr>, Box<Expr>), // target-path expr, value
    Member(Box<Expr>, Box<Expr>, bool), // obj, key, computed
    Call(Box<Expr>, Vec<Expr>),
    Func(String, Vec<String>, Vec<Stmt>),
    Ternary(Box<Expr>, Box<Expr>, Box<Expr>),
}

#[derive(Debug, Clone)]
pub enum Stmt {
    Expr(Expr),
    Var(String, Option<Expr>),
    If(Expr, Vec<Stmt>, Option<Vec<Stmt>>),
    While(Expr, Vec<Stmt>),
    For(String, Expr, Expr, Option<Expr>, Vec<Stmt>), // init var, cond, update (opt), body
    Return(Option<Expr>),
    Throw(Expr),
    Try(Vec<Stmt>, String, Vec<Stmt>),
    Block(Vec<Stmt>),
    Multi(Vec<Stmt>), // flat multi-decl — same env me (scoping preserved)
    Break,
    Continue,
}

// ======================================================== Env --
use std::cell::RefCell;
use std::rc::Rc;

#[derive(Debug)]
pub struct RefEnv {
    pub vars: RefCell<BTreeMap<String, Value>>,
    pub parent: Option<Rc<RefEnv>>,
}

impl RefEnv {
    fn new_global() -> Rc<RefEnv> {
        Rc::new(RefEnv {
            vars: RefCell::new(BTreeMap::new()),
            parent: None,
        })
    }
    fn child(parent: &Rc<RefEnv>) -> Rc<RefEnv> {
        Rc::new(RefEnv {
            vars: RefCell::new(BTreeMap::new()),
            parent: Some(parent.clone()),
        })
    }
    fn get(&self, name: &str) -> Option<Value> {
        if let Some(v) = self.vars.borrow().get(name) {
            return Some(v.clone());
        }
        if let Some(p) = &self.parent {
            return p.get(name);
        }
        None
    }
    fn set(&self, name: &str, val: Value) -> Result<(), String> {
        // existing dhoondo chain me — var-declared hi assignable (strict-lite)
        if self.set_existing(name, &val) {
            return Ok(());
        }
        let mut cur = self.vars.borrow_mut();
        cur.insert(name.to_string(), val);
        Ok(())
    }
    fn set_existing(&self, name: &str, val: &Value) -> bool {
        if self.vars.borrow().contains_key(name) {
            self.vars.borrow_mut().insert(name.to_string(), val.clone());
            return true;
        }
        if let Some(p) = &self.parent {
            return p.set_existing(name, val);
        }
        false
    }
    fn declare(&self, name: &str, val: Value) {
        self.vars.borrow_mut().insert(name.to_string(), val);
    }
}

// ======================================================== Parser --
pub struct Parser {
    toks: Vec<Tok>,
    pos: usize,
}

impl Parser {
    pub fn new(src: &str) -> Result<Self, String> {
        Ok(Parser {
            toks: lex(src)?,
            pos: 0,
        })
    }
    fn peek(&self) -> &Tok {
        self.toks.get(self.pos).unwrap_or(&Tok::Eof)
    }
    fn next(&mut self) -> Tok {
        let t = self.toks.get(self.pos).cloned().unwrap_or(Tok::Eof);
        self.pos += 1;
        t
    }
    fn eat_punct(&mut self, p: &str) -> Result<(), String> {
        match self.next() {
            Tok::Punct(s) if s == p => Ok(()),
            other => Err(format!("expected '{}', got {:?}", p, other)),
        }
    }
    fn eat_kw(&mut self, k: &str) -> Result<(), String> {
        match self.next() {
            Tok::Kw(s) if s == k => Ok(()),
            other => Err(format!("expected '{}', got {:?}", k, other)),
        }
    }
    fn is_punct(&self, p: &str) -> bool {
        matches!(self.peek(), Tok::Punct(s) if s == p)
    }
    fn is_kw(&self, k: &str) -> bool {
        matches!(self.peek(), Tok::Kw(s) if *s == k)
    }

    pub fn parse_program(&mut self) -> Result<Vec<Stmt>, String> {
        let mut out = Vec::new();
        while !matches!(self.peek(), Tok::Eof) {
            out.push(self.parse_stmt()?);
        }
        Ok(out)
    }

    fn parse_block(&mut self) -> Result<Vec<Stmt>, String> {
        self.eat_punct("{")?;
        let mut out = Vec::new();
        while !self.is_punct("}") {
            out.push(self.parse_stmt()?);
        }
        self.eat_punct("}")?;
        Ok(out)
    }

    fn parse_stmt(&mut self) -> Result<Stmt, String> {
        match self.peek().clone() {
            Tok::Punct(p) if p == "{" => Ok(Stmt::Block(self.parse_block()?)),
            Tok::Punct(p) if p == ";" => {
                self.next();
                Ok(Stmt::Expr(Expr::Undefined))
            }
            Tok::Kw(k) => match k {
                "var" | "let" | "const" => {
                    // multi-var: var a = 0, b = 1, c; — comma se chain
                    let k = k;
                    self.next();
                    let mut out = Vec::new();
                    loop {
                        let name = match self.next() {
                            Tok::Ident(s) => s,
                            other => return Err(format!("var name expected, got {:?}", other)),
                        };
                        let init = if self.is_punct("=") {
                            self.next();
                            Some(self.parse_expr()?)
                        } else {
                            None
                        };
                        out.push(Stmt::Var(name, init));
                        if self.is_punct(",") {
                            self.next();
                            continue;
                        }
                        break;
                    }
                    if out.len() == 1 {
                        self.eat_stmt_end()?;
                        return Ok(out.pop().unwrap());
                    }
                    // multi-decl flat (same env) — scoping preserved
                    let _ = k;
                    self.eat_stmt_end()?;
                    Ok(Stmt::Multi(out))
                }
                "if" => {
                    self.next();
                    self.eat_punct("(")?;
                    let cond = self.parse_expr()?;
                    self.eat_punct(")")?;
                    let then = self.parse_block()?;
                    let els = if self.is_kw("else") {
                        self.next();
                        if self.is_kw("if") {
                            Some(vec![self.parse_stmt()?])
                        } else {
                            Some(self.parse_block()?)
                        }
                    } else {
                        None
                    };
                    Ok(Stmt::If(cond, then, els))
                }
                "while" => {
                    self.next();
                    self.eat_punct("(")?;
                    let cond = self.parse_expr()?;
                    self.eat_punct(")")?;
                    Ok(Stmt::While(cond, self.parse_block()?))
                }
                "for" => {
                    self.next();
                    self.eat_punct("(")?;
                    // for (var i = 0; i < n; i = i + 1)
                    self.eat_kw("var")?;
                    let name = match self.next() {
                        Tok::Ident(s) => s,
                        other => return Err(format!("for-var expected, got {:?}", other)),
                    };
                    self.eat_punct("=")?;
                    let init = self.parse_expr()?;
                    self.eat_punct(";")?;
                    let cond = self.parse_expr()?;
                    self.eat_punct(";")?;
                    let update = if self.is_punct(")") {
                        None
                    } else {
                        Some(self.parse_expr()?)
                    };
                    self.eat_punct(")")?;
                    Ok(Stmt::For(name, init, cond, update, self.parse_block()?))
                }
                "return" => {
                    self.next();
                    let val = if self.is_punct("}") || self.is_punct(";") {
                        None
                    } else {
                        Some(self.parse_expr()?)
                    };
                    self.eat_stmt_end()?;
                    Ok(Stmt::Return(val))
                }
                "throw" => {
                    self.next();
                    Ok(Stmt::Throw(self.parse_expr()?))
                }
                "try" => {
                    self.next();
                    let body = self.parse_block()?;
                    self.eat_kw("catch")?;
                    self.eat_punct("(")?;
                    let cname = match self.next() {
                        Tok::Ident(s) => s,
                        other => return Err(format!("catch name, got {:?}", other)),
                    };
                    self.eat_punct(")")?;
                    let cbody = self.parse_block()?;
                    Ok(Stmt::Try(body, cname, cbody))
                }
                "function" => {
                    self.next();
                    let name = match self.next() {
                        Tok::Ident(s) => s,
                        other => return Err(format!("fn name, got {:?}", other)),
                    };
                    let f = self.parse_fn_tail()?;
                    Ok(Stmt::Var(name, Some(f)))
                }
                _ => {
                    let e = self.parse_expr()?;
                    self.eat_stmt_end()?;
                    Ok(Stmt::Expr(e))
                }
            },
            _ => {
                let e = self.parse_expr()?;
                self.eat_stmt_end()?;
                Ok(Stmt::Expr(e))
            }
        }
    }
    fn eat_stmt_end(&mut self) -> Result<(), String> {
        if self.is_punct(";") {
            self.next();
        }
        Ok(())
    }
    fn parse_fn_tail(&mut self) -> Result<Expr, String> {
        self.eat_punct("(")?;
        let mut params = Vec::new();
        while !self.is_punct(")") {
            match self.next() {
                Tok::Ident(s) => params.push(s),
                other => return Err(format!("param expected, got {:?}", other)),
            }
            if self.is_punct(",") {
                self.next();
            }
        }
        self.eat_punct(")")?;
        let body = self.parse_block()?;
        Ok(Expr::Func(String::new(), params, body))
    }

    // precedence: assign < ternary < or < and < eq < cmp < add < mul < unary < call/member
    pub fn parse_expr(&mut self) -> Result<Expr, String> {
        let target = self.parse_ternary()?;
        if self.is_punct("=") {
            self.next();
            let val = self.parse_expr()?;
            return Ok(Expr::Assign(String::new(), Box::new(target), Box::new(val)));
        }
        Ok(target)
    }
    fn parse_ternary(&mut self) -> Result<Expr, String> {
        let cond = self.parse_or()?;
        if self.is_punct("?") {
            self.next();
            let a = self.parse_expr()?;
            self.eat_punct(":")?;
            let b = self.parse_expr()?;
            return Ok(Expr::Ternary(Box::new(cond), Box::new(a), Box::new(b)));
        }
        Ok(cond)
    }
    fn parse_or(&mut self) -> Result<Expr, String> {
        let mut left = self.parse_and()?;
        while self.is_punct("||") {
            self.next();
            let right = self.parse_and()?;
            left = Expr::Bin("||".into(), Box::new(left), Box::new(right));
        }
        Ok(left)
    }
    fn parse_and(&mut self) -> Result<Expr, String> {
        let mut left = self.parse_eq()?;
        while self.is_punct("&&") {
            self.next();
            let right = self.parse_eq()?;
            left = Expr::Bin("&&".into(), Box::new(left), Box::new(right));
        }
        Ok(left)
    }
    fn parse_eq(&mut self) -> Result<Expr, String> {
        let mut left = self.parse_cmp()?;
        loop {
            let op = if self.is_punct("==") {
                "=="
            } else if self.is_punct("===") {
                "==="
            } else if self.is_punct("!=") {
                "!="
            } else if self.is_punct("!==") {
                "!=="
            } else {
                break;
            };
            self.next();
            let right = self.parse_cmp()?;
            left = Expr::Bin(op.into(), Box::new(left), Box::new(right));
        }
        Ok(left)
    }
    fn parse_cmp(&mut self) -> Result<Expr, String> {
        let mut left = self.parse_add()?;
        loop {
            let op = if self.is_punct("<") {
                "<"
            } else if self.is_punct(">") {
                ">"
            } else if self.is_punct("<=") {
                "<="
            } else if self.is_punct(">=") {
                ">="
            } else {
                break;
            };
            self.next();
            let right = self.parse_add()?;
            left = Expr::Bin(op.into(), Box::new(left), Box::new(right));
        }
        Ok(left)
    }
    fn parse_add(&mut self) -> Result<Expr, String> {
        let mut left = self.parse_mul()?;
        loop {
            let op = if self.is_punct("+") {
                "+"
            } else if self.is_punct("-") {
                "-"
            } else {
                break;
            };
            self.next();
            let right = self.parse_mul()?;
            left = Expr::Bin(op.into(), Box::new(left), Box::new(right));
        }
        Ok(left)
    }
    fn parse_mul(&mut self) -> Result<Expr, String> {
        let mut left = self.parse_unary()?;
        loop {
            let op = if self.is_punct("*") {
                "*"
            } else if self.is_punct("/") {
                "/"
            } else if self.is_punct("%") {
                "%"
            } else {
                break;
            };
            self.next();
            let right = self.parse_unary()?;
            left = Expr::Bin(op.into(), Box::new(left), Box::new(right));
        }
        Ok(left)
    }
    fn parse_unary(&mut self) -> Result<Expr, String> {
        let op = if self.is_punct("!") {
            "!"
        } else if self.is_punct("-") {
            "-"
        } else {
            return self.parse_call_member();
        };
        self.next();
        Ok(Expr::Un(op.into(), Box::new(self.parse_unary()?)))
    }
    fn parse_call_member(&mut self) -> Result<Expr, String> {
        let mut base = self.parse_primary()?;
        loop {
            if self.is_punct(".") {
                self.next();
                let key = match self.next() {
                    Tok::Ident(s) => s,
                    other => return Err(format!("member name, got {:?}", other)),
                };
                base = Expr::Member(Box::new(base), Box::new(Expr::Str(key)), false);
            } else if self.is_punct("[") {
                self.next();
                let key = self.parse_expr()?;
                self.eat_punct("]")?;
                base = Expr::Member(Box::new(base), Box::new(key), true);
            } else if self.is_punct("(") {
                self.next();
                let mut args = Vec::new();
                while !self.is_punct(")") {
                    args.push(self.parse_expr()?);
                    if self.is_punct(",") {
                        self.next();
                    }
                }
                self.eat_punct(")")?;
                base = Expr::Call(Box::new(base), args);
            } else {
                break;
            }
        }
        Ok(base)
    }
    fn parse_primary(&mut self) -> Result<Expr, String> {
        match self.next() {
            Tok::Num(n) => Ok(Expr::Num(n)),
            Tok::Str(s) => Ok(Expr::Str(s)),
            Tok::Kw("true") => Ok(Expr::Bool(true)),
            Tok::Kw("false") => Ok(Expr::Bool(false)),
            Tok::Kw("null") => Ok(Expr::Null),
            Tok::Kw("typeof") => Ok(Expr::Un("typeof".into(), Box::new(self.parse_unary()?))),
            Tok::Kw("function") => self.parse_fn_tail(),
            Tok::Ident(name) => Ok(Expr::Ident(name)),
            Tok::Punct(p) if p == "(" => {
                let e = self.parse_expr()?;
                self.eat_punct(")")?;
                Ok(e)
            }
            Tok::Punct(p) if p == "[" => {
                let mut items = Vec::new();
                while !self.is_punct("]") {
                    items.push(self.parse_expr()?);
                    if self.is_punct(",") {
                        self.next();
                    }
                }
                self.eat_punct("]")?;
                Ok(Expr::Arr(items))
            }
            Tok::Punct(p) if p == "{" => {
                let mut pairs = Vec::new();
                while !self.is_punct("}") {
                    let key = match self.next() {
                        Tok::Ident(s) => s,
                        Tok::Str(s) => s,
                        other => return Err(format!("obj key, got {:?}", other)),
                    };
                    self.eat_punct(":")?;
                    let val = self.parse_expr()?;
                    pairs.push((key, val));
                    if self.is_punct(",") {
                        self.next();
                    }
                }
                self.eat_punct("}")?;
                Ok(Expr::Obj(pairs))
            }
            other => Err(format!("unexpected token {:?}", other)),
        }
    }
}

// ======================================================== Interp --
pub struct Interp {
    pub global: Rc<RefEnv>,
    pub logs: RefCell<Vec<String>>, // console.log capture
    /// DOM-bridge: page-snapshot data — document.* calls isi se
    /// serve hote hain (engine M4b — JS ko real DOM dikhta hai)
    pub dom: RefCell<DomSnapshot>,
}

/// Page ka JS-visible snapshot (owned strings — lifetime-free).
#[derive(Default, Clone)]
pub struct DomSnapshot {
    pub title: String,
    pub text: String,
    pub links: Vec<(String, String)>, // href, text
    pub forms: Vec<(String, String, Vec<(String, String, String)>)>, // action, method, fields
    pub ids: BTreeMap<String, String>, // id -> text
    pub classes: BTreeMap<String, Vec<String>>, // class -> texts
}

#[derive(Debug, Clone)]
enum Flow {
    Normal,
    Expr(Value), // expr-stmt ka evaluated value (double-eval se bachne)
    Return(Option<Value>),
    Break,
    Continue,
    Throw(Value),
}

impl Interp {
    pub fn new() -> Interp {
        let g = RefEnv::new_global();
        let it = Interp {
            global: g,
            logs: RefCell::new(vec![]),
            dom: RefCell::new(DomSnapshot::default()),
        };
        it.setup_globals();
        it
    }

    /// M4b: DOM attach — is interp ko page ka snapshot do, document.*
    /// calls ab isi se resolve honge.
    pub fn attach_dom(&self, page: &crate::Page) {
        let mut d = self.dom.borrow_mut();
        d.title = page.title();
        d.text = page.text();
        d.links = page.links();
        d.forms = page.forms();
        // id/class maps — querySelector-lite + getElementsByClassName
        fn walk(n: &crate::Node, d: &mut DomSnapshot) {
            if let Some(tag) = n.tag.as_deref() {
                if let Some(id) = n.attrs.get("id") {
                    d.ids.insert(id.clone(), n.inner_text());
                }
                if let Some(cls) = n.attrs.get("class") {
                    for c in cls.split_whitespace() {
                        d.classes.entry(c.to_string()).or_default().push(n.inner_text());
                    }
                }
                let _ = tag;
            }
            for c in &n.children {
                walk(c, d);
            }
        }
        walk(&page.root, &mut d);
    }

    fn setup_globals(&self) {
        self.global.declare("console", Value::Obj(BTreeMap::new())); // special-cased in call
        self.global.declare("Math", Value::Obj(BTreeMap::new()));
        self.global.declare("JSON", Value::Obj(BTreeMap::new()));
        self.global.declare("Date", Value::Obj(BTreeMap::new()));
        // M4b DOM-bridge — __ns__ marker se get_member pehchanta hai
        let mut dobj = BTreeMap::new();
        dobj.insert("__ns__".to_string(), Value::Str("document".to_string()));
        self.global.declare("document", Value::Obj(dobj));
        self.global.declare("undefined", Value::Undefined);
        self.global.declare("NaN", Value::Num(f64::NAN));
    }

    /// Script run -> last expression value.
    pub fn run(&mut self, src: &str) -> Result<Value, String> {
        let mut p = Parser::new(src)?;
        let stmts = p.parse_program()?;
        let env = self.global.clone();
        let mut last = Value::Undefined;
        for s in &stmts {
            match self.exec(s, &env)? {
                Flow::Return(v) => return Ok(v.unwrap_or(Value::Undefined)),
                Flow::Throw(v) => return Err(format!("uncaught: {}", v)),
                Flow::Expr(v) => last = v,
                _ => {}
            }
            // last expr-stmt ka value script ka result (REPL-style)
            // NOTE: yahan RE-EVAL NAHI (Assign side-effect double hota)
            // — exec pehle hi eval kar chuka tha, value Flow::Expr me aata hai
            
        }
        Ok(last)
    }

    /// console.log ke saath run (logs copy bhi milta hai).
    pub fn run_logs(&mut self, src: &str) -> Result<(Value, Vec<String>), String> {
        let v = self.run(src)?;
        Ok((v, self.logs.borrow().clone()))
    }

    fn exec(&self, stmt: &Stmt, env: &Rc<RefEnv>) -> Result<Flow, String> {
        match stmt {
            Stmt::Block(stmts) => {
                let inner = RefEnv::child(env);
                for s in stmts {
                    match self.exec(s, &inner)? {
                        Flow::Normal | Flow::Continue | Flow::Break => {}
                        f => return Ok(f),
                    }
                }
                Ok(Flow::Normal)
            }
            Stmt::Expr(e) => {
                let v = self.eval(e, env)?;
                Ok(Flow::Expr(v))
            }
            Stmt::Var(name, init) => {
                let v = match init {
                    Some(e) => self.eval(e, env)?,
                    None => Value::Undefined,
                };
                env.declare(name, v);
                Ok(Flow::Normal)
            }
            Stmt::If(cond, then, els) => {
                if self.eval(cond, env)?.truthy() {
                    for s in then {
                        let f = self.exec(s, env)?;
                        if !matches!(f, Flow::Normal) {
                            return Ok(f);
                        }
                    }
                } else if let Some(els) = els {
                    for s in els {
                        let f = self.exec(s, env)?;
                        if !matches!(f, Flow::Normal) {
                            return Ok(f);
                        }
                    }
                }
                Ok(Flow::Normal)
            }
            Stmt::While(cond, body) => {
                let mut guard = 0u64;
                while self.eval(cond, env)?.truthy() {
                    guard += 1;
                    if guard > 1_000_000 {
                        return Err("while-loop guard (1M iter) hit".into());
                    }
                    let inner = RefEnv::child(env);
                    for s in body {
                        match self.exec(s, &inner)? {
                            Flow::Break => return Ok(Flow::Normal),
                            Flow::Return(v) => return Ok(Flow::Return(v)),
                            Flow::Throw(v) => return Ok(Flow::Throw(v)),
                            _ => {}
                        }
                    }
                }
                Ok(Flow::Normal)
            }
            Stmt::For(name, init, cond, update, body) => {
                let iv = self.eval(init, env)?;
                env.declare(name, iv);
                let mut guard = 0u64;
                while self.eval(cond, env)?.truthy() {
                    guard += 1;
                    if guard > 1_000_000 {
                        return Err("for-loop guard (1M iter) hit".into());
                    }
                    let inner = RefEnv::child(env);
                    for s in body {
                        match self.exec(s, &inner)? {
                            Flow::Break => return Ok(Flow::Normal),
                            Flow::Return(v) => return Ok(Flow::Return(v)),
                            Flow::Throw(v) => return Ok(Flow::Throw(v)),
                            _ => {}
                        }
                    }
                    // update: i = i + 1 — Assign expr eval (side-effect env.set)
                    if let Some(u) = update {
                        self.eval(u, env)?;
                    }
                }
                Ok(Flow::Normal)
            }
            Stmt::Return(e) => {
                let v = match e {
                    Some(ex) => Some(self.eval(ex, env)?),
                    None => None,
                };
                Ok(Flow::Return(v))
            }
            Stmt::Throw(e) => Ok(Flow::Throw(self.eval(e, env)?)),
            Stmt::Try(body, cname, cbody) => {
                match (|| -> Result<Flow, String> {
                    let inner = RefEnv::child(env);
                    for s in body {
                        match self.exec(s, &inner)? {
                            Flow::Normal | Flow::Continue | Flow::Break => {}
                            f => return Ok(f),
                        }
                    }
                    Ok(Flow::Normal)
                })() {
                    Ok(Flow::Throw(v)) => {
                        let inner = RefEnv::child(env);
                        inner.declare(cname, v);
                        for s in cbody {
                            match self.exec(s, &inner)? {
                                Flow::Normal | Flow::Continue | Flow::Break => {}
                                f => return Ok(f),
                            }
                        }
                        Ok(Flow::Normal)
                    }
                    Ok(f) => Ok(f),
                    Err(e) => {
                        // native error ko bhi catch
                        let inner = RefEnv::child(env);
                        inner.declare(cname, Value::Str(e));
                        for s in cbody {
                            match self.exec(s, &inner)? {
                                Flow::Normal | Flow::Continue | Flow::Break => {}
                                f => return Ok(f),
                            }
                        }
                        Ok(Flow::Normal)
                    }
                }
            }
            Stmt::Multi(stmts) => {
                for s in stmts {
                    match self.exec(s, env)? {
                        Flow::Normal | Flow::Continue | Flow::Break => {}
                        f => return Ok(f),
                    }
                }
                Ok(Flow::Normal)
            }
            Stmt::Break => Ok(Flow::Break),
            Stmt::Continue => Ok(Flow::Continue),
        }
    }

    fn eval(&self, e: &Expr, env: &Rc<RefEnv>) -> Result<Value, String> {
        match e {
            Expr::Num(n) => Ok(Value::Num(*n)),
            Expr::Str(s) => Ok(Value::Str(s.clone())),
            Expr::Bool(b) => Ok(Value::Bool(*b)),
            Expr::Null => Ok(Value::Null),
            Expr::Undefined => Ok(Value::Undefined),
            Expr::Ident(name) => env
                .get(name)
                .ok_or_else(|| format!("'{}' is not defined", name)),
            Expr::Arr(items) => {
                let mut out = Vec::new();
                for i in items {
                    out.push(self.eval(i, env)?);
                }
                Ok(Value::Arr(out))
            }
            Expr::Obj(pairs) => {
                let mut m = BTreeMap::new();
                for (k, v) in pairs {
                    m.insert(k.clone(), self.eval(v, env)?);
                }
                Ok(Value::Obj(m))
            }
            Expr::Func(_, params, body) => Ok(Value::Func(RcFunc {
                name: String::new(),
                params: params.clone(),
                body: body.clone(),
                env: env.clone(),
            })),
            Expr::Ternary(c, a, b) => {
                if self.eval(c, env)?.truthy() {
                    self.eval(a, env)
                } else {
                    self.eval(b, env)
                }
            }
            Expr::Un(op, x) => {
                let v = self.eval(x, env)?;
                match op.as_str() {
                    "!" => Ok(Value::Bool(!v.truthy())),
                    "-" => match v {
                        Value::Num(n) => Ok(Value::Num(-n)),
                        other => Err(format!("cannot negate {}", other.type_name())),
                    },
                    "typeof" => Ok(Value::Str(v.type_name().to_string())),
                    other => Err(format!("bad unary {}", other)),
                }
            }
            Expr::Bin(op, a, b) => {
                // short-circuit
                if op == "&&" {
                    let l = self.eval(a, env)?;
                    if !l.truthy() {
                        return Ok(l);
                    }
                    return self.eval(b, env);
                }
                if op == "||" {
                    let l = self.eval(a, env)?;
                    if l.truthy() {
                        return Ok(l);
                    }
                    return self.eval(b, env);
                }
                let l = self.eval(a, env)?;
                // member-assign path: a.b = ... handled in Assign
                let r = self.eval(b, env)?;
                match op.as_str() {
                    "+" => match (&l, &r) {
                        (Value::Str(a), _) => Ok(Value::Str(format!("{}{}", a, r))),
                        (_, Value::Str(b)) => Ok(Value::Str(format!("{}{}", l, b))),
                        (Value::Num(a), Value::Num(b)) => Ok(Value::Num(a + b)),
                        _ => Ok(Value::Str(format!("{}{}", l, r))),
                    },
                    "-" | "*" | "/" | "%" => {
                        let (a, b) = match (&l, &r) {
                            (Value::Num(a), Value::Num(b)) => (*a, *b),
                            _ => {
                                return Err(format!(
                                    "{} on non-numbers ({} {})",
                                    op,
                                    l.type_name(),
                                    r.type_name()
                                ))
                            }
                        };
                        Ok(Value::Num(match op.as_str() {
                            "-" => a - b,
                            "*" => a * b,
                            "/" => a / b,
                            _ => a % b,
                        }))
                    }
                    "==" => Ok(Value::Bool(l.eq_loose(&r))),
                    "===" => Ok(Value::Bool(l.eq_loose(&r))),
                    "!=" => Ok(Value::Bool(!l.eq_loose(&r))),
                    "!==" => Ok(Value::Bool(!l.eq_loose(&r))),
                    "<" => self.num_cmp(&l, &r, |a, b| a < b),
                    ">" => self.num_cmp(&l, &r, |a, b| a > b),
                    "<=" => self.num_cmp(&l, &r, |a, b| a <= b),
                    ">=" => self.num_cmp(&l, &r, |a, b| a >= b),
                    _ => Err(format!("bad binop {}", op)),
                }
            }
            Expr::Member(obj, key, computed) => {
                let o = self.eval(obj, env)?;
                let k = if *computed {
                    self.eval(key, env)?
                } else {
                    self.eval(key, env)?
                };
                self.get_member(&o, &k)
            }
            Expr::Call(target, args) => {
                // console.log / Math.x / JSON.x special surfaces
                if let Expr::Member(ob, key, _) = &**target {
                    let mut tried_builtin = false;
                    if let Expr::Ident(ns) = &**ob {
                        let mut argv = Vec::new();
                        for a in args {
                            argv.push(self.eval(a, env)?);
                        }
                        if let Some(v) = self.call_builtin(ns, &key_str(key), &argv)? {
                            return Ok(v);
                        }
                        tried_builtin = true;
                    }
                    if tried_builtin || !matches!(&**ob, Expr::Ident(_)) {
                        // VALUE-method fallback: builtin miss ya non-ns member
                        // (a.map, b.join, s.slice — obj eval + call_method)
                        let o = self.eval(ob, env)?;
                        let m = key_str(key);
                        if !m.is_empty() {
                            if let Ok(v) = self.call_method(o, &m, &argv_prebuilt(args, env, self)?) {
                                return Ok(v);
                            }
                        }
                    }
                    else {
                        // VALUE-method: a.map(...) / s.slice(...) — obj eval
                        // karke call_method dispatch (arr/str methods)
                        let o = self.eval(ob, env)?;
                        let m = key_str(key);
                        let mut argv = Vec::new();
                        for a in args {
                            argv.push(self.eval(a, env)?);
                        }
                        if !m.is_empty() {
                            if let Ok(v) = self.call_method(o.clone(), &m, &argv) {
                                return Ok(v);
                            }
                        }
                    }
                }
                // user function
                let f = self.eval(target, env)?;
                let mut argv = Vec::new();
                for a in args {
                    argv.push(self.eval(a, env)?);
                }
                self.call_func(f, argv)
            }
            Expr::Assign(_, target, val) => {
                let v = self.eval(val, env)?;
                match &**target {
                    Expr::Ident(name) => {
                        env.set(name, v.clone())?;
                        Ok(v)
                    }
                    Expr::Member(obj, key, computed) => {
                        let o = self.eval(obj, env)?;
                        let k = self.eval(key, env)?;
                        let _ = computed;
                        self.set_member(o, &k, v.clone())?;
                        Ok(v)
                    }
                    other => Err(format!("bad assign target {:?}", other)),
                }
            }
        }
    }

    fn num_cmp(&self, l: &Value, r: &Value, f: impl Fn(f64, f64) -> bool) -> Result<Value, String> {
        let a = match l {
            Value::Num(n) => *n,
            Value::Str(s) => s.parse().unwrap_or(f64::NAN),
            Value::Bool(b) => *b as i64 as f64,
            _ => return Err(format!("compare on {}", l.type_name())),
        };
        let b = match r {
            Value::Num(n) => *n,
            Value::Str(s) => s.parse().unwrap_or(f64::NAN),
            Value::Bool(b) => *b as i64 as f64,
            _ => return Err(format!("compare on {}", r.type_name())),
        };
        Ok(Value::Bool(f(a, b)))
    }

    fn get_member(&self, o: &Value, k: &Value) -> Result<Value, String> {
        // M4b: document.title/body/links/forms — property-GET path
        if let (Value::Obj(m), Value::Str(key)) = (o, k) {
            if m.get("__ns__").and_then(|v| match v {
                Value::Str(s) => Some(s.as_str()),
                _ => None,
            }) == Some("document")
            {
                if let Some(v) = self.dom_prop(key)? {
                    return Ok(v);
                }
            }
            return Ok(m.get(key).cloned().unwrap_or(Value::Undefined));
        }
        match (o, k) {
            (Value::Obj(m), Value::Str(key)) => Ok(m.get(key).cloned().unwrap_or(Value::Undefined)),
            (Value::Arr(a), Value::Num(i)) => Ok(a
                .get(*i as usize)
                .cloned()
                .unwrap_or(Value::Undefined)),
            (Value::Arr(a), Value::Str(key)) => match key.as_str() {
                "length" => Ok(Value::Num(a.len() as f64)),
                _ => Ok(Value::Undefined),
            },
            (Value::Str(s), Value::Str(key)) => match key.as_str() {
                "length" => Ok(Value::Num(s.chars().count() as f64)),
                _ => Ok(Value::Undefined),
            },
            _ => Ok(Value::Undefined),
        }
    }

    fn set_member(&self, o: Value, k: &Value, v: Value) -> Result<(), String> {
        match o {
            Value::Obj(m) => {
                // BTreeMap me shared-mut nahi (value clone) — engine me obj
                // by-value hai; assignments original scope me propagate nahi
                // (known-gap: object mutation via member assign limited)
                let mut m = m;
                if let Value::Str(key) = k {
                    m.insert(key.clone(), v);
                }
                // NOTE: ye mutlocalized copy hai — real mutation M4b me RefCell
                // obj-model ke saath. Honest gap.
                Ok(())
            }
            Value::Arr(mut a) => {
                if let Value::Num(i) = k {
                    let idx = *i as usize;
                    while a.len() <= idx {
                        a.push(Value::Undefined);
                    }
                    a[idx] = v;
                }
                Ok(())
            }
            _ => Err("assign on non-object".into()),
        }
    }

    fn call_func(&self, f: Value, args: Vec<Value>) -> Result<Value, String> {
        match f {
            Value::Func(rc) => {
                let inner = RefEnv::child(&rc.env);
                for (i, p) in rc.params.iter().enumerate() {
                    inner.declare(p, args.get(i).cloned().unwrap_or(Value::Undefined));
                }
                for s in &rc.body {
                    match self.exec(s, &inner)? {
                        Flow::Return(v) => return Ok(v.unwrap_or(Value::Undefined)),
                        Flow::Throw(v) => return Err(format!("uncaught in fn: {}", v)),
                        _ => {}
                    }
                }
                Ok(Value::Undefined)
            }
            other => Err(format!("not a function: {}", other.type_name())),
        }
    }

    /// document.* property-GET serve (call_builtin bhi isi se
    /// value-build karta hai — ek hi data source)
    fn dom_prop(&self, key: &str) -> Result<Option<Value>, String> {
        match key {
            "title" => Ok(Some(Value::Str(self.dom.borrow().title.clone()))),
            "body" => Ok(Some(Value::Str(self.dom.borrow().text.clone()))),
            "links" => {
                let d = self.dom.borrow();
                let arr: Vec<Value> = d
                    .links
                    .iter()
                    .map(|(h, t)| {
                        let mut o = BTreeMap::new();
                        o.insert("href".to_string(), Value::Str(h.clone()));
                        o.insert("text".to_string(), Value::Str(t.clone()));
                        Value::Obj(o)
                    })
                    .collect();
                Ok(Some(Value::Arr(arr)))
            }
            "forms" => {
                let d = self.dom.borrow();
                let arr: Vec<Value> = d
                    .forms
                    .iter()
                    .map(|(a, m, fields)| {
                        let mut o = BTreeMap::new();
                        o.insert("action".to_string(), Value::Str(a.clone()));
                        o.insert("method".to_string(), Value::Str(m.clone()));
                        let fs: Vec<Value> = fields
                            .iter()
                            .map(|(n, t, v)| {
                                let mut fo = BTreeMap::new();
                                fo.insert("name".to_string(), Value::Str(n.clone()));
                                fo.insert("type".to_string(), Value::Str(t.clone()));
                                fo.insert("value".to_string(), Value::Str(v.clone()));
                                Value::Obj(fo)
                            })
                            .collect();
                        o.insert("fields".to_string(), Value::Arr(fs));
                        Value::Obj(o)
                    })
                    .collect();
                Ok(Some(Value::Arr(arr)))
            }
            _ => Ok(None), // querySelector etc — call path
        }
    }

    /// console/Math/JSON/Date builtins — None = user-fn path
    fn call_builtin(&self, ns: &str, method: &str, args: &[Value]) -> Result<Option<Value>, String> {
        let bad = |m: &str| -> Result<Option<Value>, String> {
            Err(format!("{}.{} args bad", ns, m))
        };
        match (ns, method) {
            // ---------------- M4b: DOM-bridge (document.*) ----------------
            ("document", "title") => Ok(Some(Value::Str(self.dom.borrow().title.clone()))),
            ("document", "body") => {
                // body text (rendered) — common scraping pattern
                let t = self.dom.borrow().text.clone();
                Ok(Some(Value::Str(t)))
            }
            ("document", "links") => {
                let d = self.dom.borrow();
                let arr: Vec<Value> = d
                    .links
                    .iter()
                    .map(|(h, t)| {
                        let mut o = BTreeMap::new();
                        o.insert("href".to_string(), Value::Str(h.clone()));
                        o.insert("text".to_string(), Value::Str(t.clone()));
                        Value::Obj(o)
                    })
                    .collect();
                Ok(Some(Value::Arr(arr)))
            }
            ("document", "forms") => {
                let d = self.dom.borrow();
                let arr: Vec<Value> = d
                    .forms
                    .iter()
                    .map(|(a, m, fields)| {
                        let mut o = BTreeMap::new();
                        o.insert("action".to_string(), Value::Str(a.clone()));
                        o.insert("method".to_string(), Value::Str(m.clone()));
                        let fs: Vec<Value> = fields
                            .iter()
                            .map(|(n, t, v)| {
                                let mut fo = BTreeMap::new();
                                fo.insert("name".to_string(), Value::Str(n.clone()));
                                fo.insert("type".to_string(), Value::Str(t.clone()));
                                fo.insert("value".to_string(), Value::Str(v.clone()));
                                Value::Obj(fo)
                            })
                            .collect();
                        o.insert("fields".to_string(), Value::Arr(fs));
                        Value::Obj(o)
                    })
                    .collect();
                Ok(Some(Value::Arr(arr)))
            }
            ("document", "querySelector") => {
                // "#id" | ".class" | "tag" — first match text ya null
                let sel = match args.first() {
                    Some(Value::Str(s)) => s.clone(),
                    _ => return Ok(Some(Value::Null)),
                };
                let d = self.dom.borrow();
                if let Some(id) = sel.strip_prefix('#') {
                    return Ok(match d.ids.get(id) {
                        Some(t) => Some(Value::Str(t.clone())),
                        None => Some(Value::Null),
                    });
                }
                if let Some(cls) = sel.strip_prefix('.') {
                    return Ok(match d.classes.get(cls).and_then(|v| v.first()) {
                        Some(t) => Some(Value::Str(t.clone())),
                        None => Some(Value::Null),
                    });
                }
                // tag: title special-case, baaki me text-scan nahi — null
                if sel == "title" {
                    return Ok(Some(Value::Str(d.title.clone())));
                }
                Ok(Some(Value::Null))
            }
            ("document", "getElementsByClassName") => {
                let cls = match args.first() {
                    Some(Value::Str(s)) => s.clone(),
                    _ => return Ok(Some(Value::Arr(vec![]))),
                };
                let texts = self
                    .dom
                    .borrow()
                    .classes
                    .get(&cls)
                    .cloned()
                    .unwrap_or_default();
                Ok(Some(Value::Arr(texts.into_iter().map(Value::Str).collect())))
            }
            // ---------------- end DOM-bridge ----------------
            ("console", "log") => {
                let line = args
                    .iter()
                    .map(|v| v.to_string())
                    .collect::<Vec<_>>()
                    .join(" ");
                self.logs.borrow_mut().push(line);
                Ok(Some(Value::Undefined))
            }
            ("Math", "min") => {
                let mut best = f64::INFINITY;
                for a in args {
                    match a {
                        Value::Num(n) => best = best.min(*n),
                        _ => return bad("min"),
                    }
                }
                Ok(Some(Value::Num(best)))
            }
            ("Math", "max") => {
                let mut best = f64::NEG_INFINITY;
                for a in args {
                    match a {
                        Value::Num(n) => best = best.max(*n),
                        _ => return bad("max"),
                    }
                }
                Ok(Some(Value::Num(best)))
            }
            ("Math", "abs") => match args {
                [Value::Num(n)] => Ok(Some(Value::Num(n.abs()))),
                _ => bad("abs"),
            },
            ("Math", "floor") => match args {
                [Value::Num(n)] => Ok(Some(Value::Num(n.floor()))),
                _ => bad("floor"),
            },
            ("JSON", "stringify") => match args.first() {
                Some(v) => Ok(Some(Value::Str(json_stringify(v)))),
                None => bad("stringify"),
            },
            ("JSON", "parse") => match args.first() {
                Some(Value::Str(s)) => {
                    let mut p = JParser {
                        b: s.chars().collect(),
                        i: 0,
                    };
                    p.ws();
                    let v = p.value().map_err(|e| format!("JSON.parse: {}", e))?;
                    Ok(Some(v))
                }
                _ => bad("parse"),
            },
            ("Date", "now") => {
                let ms = std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .map(|d| d.as_millis() as f64)
                    .unwrap_or(0.0);
                Ok(Some(Value::Num(ms)))
            }
            _ => Ok(None),
        }
    }

    /// String/Array methods — .method(...) call form on values
    /// (js-web DOM layer isko extend karega)
    pub fn call_method(&self, o: Value, method: &str, args: &[Value]) -> Result<Value, String> {
        match (&o, method) {
            (Value::Str(s), "indexOf") => {
                // JS indexOf: substring-search, nahi mila to -1
                let needle = match args.first() {
                    Some(Value::Str(n)) => n.clone(),
                    _ => String::new(),
                };
                Ok(Value::Num(match s.find(&needle) {
                    Some(b) => s[..b].chars().count() as f64,
                    None => -1.0,
                }))
            }
            (Value::Arr(a), "indexOf") => {
                let target = args.first().cloned().unwrap_or(Value::Undefined);
                for (i, item) in a.iter().enumerate() {
                    if *item == target {
                        return Ok(Value::Num(i as f64));
                    }
                }
                Ok(Value::Num(-1.0))
            }
            (Value::Str(s), "toUpperCase") => Ok(Value::Str(s.to_uppercase())),
            (Value::Str(s), "toLowerCase") => Ok(Value::Str(s.to_lowercase())),
            (Value::Str(s), "slice") => {
                let (a, b) = match (args.get(0), args.get(1)) {
                    (Some(Value::Num(a)), Some(Value::Num(b))) => (*a as usize, *b as usize),
                    (Some(Value::Num(a)), None) => (*a as usize, s.len()),
                    _ => return Err("slice args".into()),
                };
                Ok(Value::Str(s.chars().skip(a).take(b - a).collect()))
            }
            (Value::Arr(a), "push") => {
                let mut a = a.clone();
                for v in args {
                    a.push(v.clone());
                }
                Ok(Value::Arr(a))
            }
            (Value::Arr(a), "join") => {
                let sep = match args.first() {
                    Some(Value::Str(s)) => s.clone(),
                    _ => ",".to_string(),
                };
                let parts: Vec<String> = a.iter().map(|v| v.to_string()).collect();
                Ok(Value::Str(parts.join(&sep)))
            }
            (Value::Arr(a), "map") => {
                let f = args
                    .first()
                    .cloned()
                    .ok_or("map needs fn")?;
                let mut out = Vec::new();
                for (i, item) in a.iter().enumerate() {
                    out.push(self.call_func(f.clone(), vec![item.clone(), Value::Num(i as f64)])?);
                }
                Ok(Value::Arr(out))
            }
            (Value::Arr(a), "forEach") => {
                let f = args.first().cloned().ok_or("forEach needs fn")?;
                for (i, item) in a.iter().enumerate() {
                    self.call_func(f.clone(), vec![item.clone(), Value::Num(i as f64)])?;
                }
                Ok(Value::Undefined)
            }
            (Value::Arr(a), "filter") => {
                let f = args.first().cloned().ok_or("filter needs fn")?;
                let mut out = Vec::new();
                for (i, item) in a.iter().enumerate() {
                    let keep = self.call_func(f.clone(), vec![item.clone(), Value::Num(i as f64)])?;
                    if keep.truthy() {
                        out.push(item.clone());
                    }
                }
                Ok(Value::Arr(out))
            }
            _ => Err(format!("method {} on {}", method, o.type_name())),
        }
    }
}

fn key_str(e: &Expr) -> String {
    match e {
        Expr::Str(s) | Expr::Ident(s) => s.clone(),
        _ => String::new(),
    }
}

fn dummy_update_fix(_name: &str, _body: &[Stmt]) -> Expr {
    // for-update parser me Assign expr hi hai (i = i + 1) — wo Stmt::Expr
    // ke through eval hota hai. Ye helper legacy/pad tha — unused.
    Expr::Undefined
}

// ================================================= JSON (stringify+parse) --
pub fn json_stringify(v: &Value) -> String {
    let mut s = String::new();
    json_write(v, &mut s);
    s
}

fn json_write(v: &Value, out: &mut String) {
    match v {
        Value::Undefined | Value::Null => out.push_str("null"),
        Value::Bool(b) => {
            let _ = write!(out, "{}", b);
        }
        Value::Num(n) => {
            if n.fract() == 0.0 && n.abs() < 1e15 {
                let _ = write!(out, "{}", *n as i64);
            } else {
                let _ = write!(out, "{}", n);
            }
        }
        Value::Str(s) => {
            out.push('"');
            for c in s.chars() {
                match c {
                    '"' => out.push_str("\\\""),
                    '\\' => out.push_str("\\\\"),
                    '\n' => out.push_str("\\n"),
                    '\t' => out.push_str("\\t"),
                    _ => out.push(c),
                }
            }
            out.push('"');
        }
        Value::Arr(a) => {
            out.push('[');
            for (i, item) in a.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                json_write(item, out);
            }
            out.push(']');
        }
        Value::Obj(m) => {
            out.push('{');
            for (i, (k, val)) in m.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                let _ = write!(out, "\"{}\":", k);
                json_write(val, out);
            }
            out.push('}');
        }
        Value::Func(_) => out.push_str("null"),
    }
}

struct JParser {
    b: Vec<char>,
    i: usize,
}

impl JParser {
    fn ws(&mut self) {
        while self.i < self.b.len() && self.b[self.i].is_whitespace() {
            self.i += 1;
        }
    }
    fn value(&mut self) -> Result<Value, String> {
        self.ws();
        match self.b.get(self.i) {
            Some('{') => {
                self.i += 1;
                let mut m = BTreeMap::new();
                self.ws();
                if self.b.get(self.i) == Some(&'}') {
                    self.i += 1;
                    return Ok(Value::Obj(m));
                }
                loop {
                    self.ws();
                    let key = match self.value()? {
                        Value::Str(s) => s,
                        _ => return Err("obj key expected".into()),
                    };
                    self.ws();
                    if self.b.get(self.i) != Some(&':') {
                        return Err("':' expected".into());
                    }
                    self.i += 1;
                    let v = self.value()?;
                    m.insert(key, v);
                    self.ws();
                    match self.b.get(self.i) {
                        Some(',') => {
                            self.i += 1;
                        }
                        Some('}') => {
                            self.i += 1;
                            return Ok(Value::Obj(m));
                        }
                        _ => return Err("',' or '}' expected".into()),
                    }
                }
            }
            Some('[') => {
                self.i += 1;
                let mut a = Vec::new();
                self.ws();
                if self.b.get(self.i) == Some(&']') {
                    self.i += 1;
                    return Ok(Value::Arr(a));
                }
                loop {
                    a.push(self.value()?);
                    self.ws();
                    match self.b.get(self.i) {
                        Some(',') => {
                            self.i += 1;
                        }
                        Some(']') => {
                            self.i += 1;
                            return Ok(Value::Arr(a));
                        }
                        _ => return Err("',' or ']' expected".into()),
                    }
                }
            }
            Some('"') => {
                self.i += 1;
                let mut s = String::new();
                while let Some(&c) = self.b.get(self.i) {
                    self.i += 1;
                    match c {
                        '"' => return Ok(Value::Str(s)),
                        '\\' => {
                            match self.b.get(self.i) {
                                Some('n') => s.push('\n'),
                                Some('t') => s.push('\t'),
                                Some(&e) => s.push(e),
                                None => {}
                            }
                            self.i += 1;
                        }
                        _ => s.push(c),
                    }
                }
                Err("unterminated string".into())
            }
            Some('t') => {
                self.expect_lit("true")?;
                Ok(Value::Bool(true))
            }
            Some('f') => {
                self.expect_lit("false")?;
                Ok(Value::Bool(false))
            }
            Some('n') => {
                self.expect_lit("null")?;
                Ok(Value::Null)
            }
            Some(c) if c.is_ascii_digit() || *c == '-' => {
                let s = self.i;
                if *c == '-' {
                    self.i += 1;
                }
                while self.i < self.b.len()
                    && (self.b[self.i].is_ascii_digit() || self.b[self.i] == '.')
                {
                    self.i += 1;
                }
                let txt: String = self.b[s..self.i].iter().collect();
                txt.parse().map(Value::Num).map_err(|e| format!("{}", e))
            }
            other => Err(format!("unexpected {:?}", other)),
        }
    }
    fn expect_lit(&mut self, lit: &str) -> Result<(), String> {
        for c in lit.chars() {
            if self.b.get(self.i) != Some(&c) {
                return Err(format!("expected {}", lit));
            }
            self.i += 1;
        }
        Ok(())
    }
}

// ======================================================== tests --
fn argv_prebuilt(args: &[Expr], env: &Rc<RefEnv>, it: &Interp) -> Result<Vec<Value>, String> {
    let mut out = Vec::new();
    for a in args {
        out.push(it.eval(a, env)?);
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn run(src: &str) -> Interp {
        let mut it = Interp::new();
        it.run(src).expect("run ok");
        it
    }
    fn v(src: &str) -> Value {
        let mut it = Interp::new();
        it.run(src).expect("run ok")
    }
    fn logs(src: &str) -> Vec<String> {
        let mut it = Interp::new();
        it.run(src).expect("run ok");
        let x = it.logs.borrow().clone();
        x
    }

    #[test]
    fn arith_precedence() {
        assert_eq!(v("1 + 2 * 3"), Value::Num(7.0));
        assert_eq!(v("(1 + 2) * 3"), Value::Num(9.0));
        assert_eq!(v("10 % 3"), Value::Num(1.0));
        assert_eq!(v("2 - 3 - 4"), Value::Num(-5.0));
    }

    #[test]
    fn string_concat() {
        assert_eq!(v("'a' + 'b'"), Value::Str("ab".into()));
        assert_eq!(v("'n=' + 5"), Value::Str("n=5".into()));
    }

    #[test]
    fn vars_and_assign() {
        assert_eq!(
            v("var x = 10; x = x + 5; x"),
            Value::Num(15.0)
        );
    }

    #[test]
    fn closures() {
        let r = v(
            "function mk(a) { return function(b) { return a + b; }; } \
             var add3 = mk(3); add3(4)",
        );
        assert_eq!(r, Value::Num(7.0));
    }

    #[test]
    fn higher_order_fns() {
        let r = v(
            "function twice(f, x) { return f(f(x)); } \
             function inc(n) { return n + 1; } \
             twice(inc, 10)",
        );
        assert_eq!(r, Value::Num(12.0));
    }

    #[test]
    fn objects_and_members() {
        let r = v("var o = {name: 'ghost', n: 42}; o.name + '!' + o.n");
        assert_eq!(r, Value::Str("ghost!42".into()));
    }

    #[test]
    fn arrays_and_methods() {
        let r = v(
            "var a = [1, 2, 3, 4]; \
             var b = a.map(function(x) { return x * 2; }); \
             b.join('-')",
        );
        assert_eq!(r, Value::Str("2-4-6-8".into()));
    }

    #[test]
    fn array_filter_foreach() {
        let it = run(
            "var s = 0; \
             [1,2,3,4].filter(function(x){ return x % 2 == 0; })\
               .forEach(function(x){ s = s + x; }); \
             console.log(s);",
        );
        let lg = it.logs.borrow().clone();
        assert_eq!(lg[0], "6");
    }

    #[test]
    fn json_roundtrip() {
        let r = v(
            "var o = {a: 1, b: [true, 'x'], c: {d: null}}; \
             var s = JSON.stringify(o); \
             var p = JSON.parse(s); \
             p.b[1] + p.a",
        );
        // p.a number concat (Str+Num)
        assert_eq!(r, Value::Str("x1".into()));
    }

    #[test]
    fn string_methods() {
        assert_eq!(v("'abc'.length"), Value::Num(3.0));
    }

    #[test]
    fn ternary_and_logic() {
        assert_eq!(v("1 < 2 ? 'yes' : 'no'"), Value::Str("yes".into()));
        assert_eq!(v("false || 'fallback'"), Value::Str("fallback".into()));
        assert_eq!(v("0 && 'never'"), Value::Num(0.0));
        assert_eq!(v("!0"), Value::Bool(true));
    }

    #[test]
    fn fib_loop() {
        let r = v(
            "var a = 0, b = 1, i = 0; \
             for (var k = 0; k < 10; k = k + 1) { \
                 var t = a + b; a = b; b = t; \
             } \
             a",
        );
        assert_eq!(r, Value::Num(55.0));
    }

    #[test]
    fn while_loop() {
        let r = v(
            "var i = 0, s = ''; \
             while (i < 3) { s = s + i; i = i + 1; } \
             s",
        );
        assert_eq!(r, Value::Str("012".into()));
    }

    #[test]
    fn try_catch_throw() {
        let logs = logs(
            "try { throw 'boom'; } catch (e) { console.log('caught:' + e); } \
             console.log('after');",
        );
        assert_eq!(logs[0], "caught:boom");
        assert_eq!(logs[1], "after");
    }

    #[test]
    fn typeof_kw() {
        assert_eq!(v("typeof 1"), Value::Str("number".into()));
        assert_eq!(v("typeof 'x'"), Value::Str("string".into()));
        assert_eq!(v("typeof {}"), Value::Str("object".into()));
    }

    #[test]
    fn console_log_capture() {
        let logs = logs("console.log('hello', 42, [1,2]);");
        assert_eq!(logs[0], "hello 42 [1,2]");
    }

    #[test]
    fn dom_bridge_scraping() {
        // M4b: attach_dom ke baad document.* JS se real page dikhta hai
        let html = "<html><head><title>RE Target</title></head>\
            <body><a href='/a'>Alpha</a><a href='/b'>Beta</a>\
            <div id='status'>walled</div><div class='wall'>Turnstile</div>\
            <form action='/login' method='POST'><input name='email'/></form>\
            </body></html>";
        let page = crate::Page::parse(html);
        let mut it = Interp::new();
        it.attach_dom(&page);

        assert_eq!(it.run("document.title").unwrap(), Value::Str("RE Target".into()));
        let links = it.run("document.links.length").unwrap();
        assert_eq!(links, Value::Num(2.0));
        let first_href = it.run("document.links[0].href").unwrap();
        assert_eq!(first_href, Value::Str("/a".into()));
        let q = it.run("document.querySelector('#status')").unwrap();
        assert_eq!(q, Value::Str("walled".into()));
        let cls = it.run("document.querySelector('.wall')").unwrap();
        assert_eq!(cls, Value::Str("Turnstile".into()));
        let forms = it.run("document.forms.length").unwrap();
        assert_eq!(forms, Value::Num(1.0));
        let act = it.run("document.forms[0].action").unwrap();
        assert_eq!(act, Value::Str("/login".into()));
    }

    #[test]
    fn dom_bridge_js_math_scrape() {
        // end-to-end: DOM data + JS logic combine — wall-scan script
        let html = "<title>Chk</title><div class='cf'>challenge</div>\
            <form action='/verify'><input name='cf-turnstile-response'/></form>";
        let page = crate::Page::parse(html);
        let mut it = Interp::new();
        it.attach_dom(&page);
        let v = it
            .run(
                "var w = document.querySelector('.cf'); \
                 var f = document.forms[0]; \
                 var bad = f.fields[0].name; \
                 if (w != null && bad.indexOf('turnstile') >= 0) { 'WALLED' } else { 'clear' }",
            )
            .unwrap();
        assert_eq!(v, Value::Str("WALLED".into()));
    }

    #[test]
    fn json_stringify_escape() {
        let mut it = Interp::new();
        let out = json_stringify(&Value::Str("a\"b\\c\nd".into()));
        assert_eq!(out, "\"a\\\"b\\\\c\\nd\"");
        let parsed = it.run("JSON.parse('\"a\\\\nb\"')").unwrap();
        assert_eq!(parsed, Value::Str("a\nb".into()));
    }
}
