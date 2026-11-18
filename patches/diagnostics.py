#!/usr/bin/env python3
"""Diagnostics for tracking down guest crashes (HyperHLE core).

Run from the root of an Applesauce checkout (tag ios-v0.4.1).

  * dyld keeps a ring buffer of the last 32 host-function calls (symbol,
    SVC pc, guest lr);
  * the first NULL-page accesses and the first "Ignored UndefinedInstruction"
    of each site dump that buffer (plus registers for the latter), so the log
    shows which API the guest called right before going wrong;
  * every Audio*/ExtAudio* host call that returns a non-zero OSStatus is
    logged (capped), since games built on Apple's CA* C++ helpers turn any
    such error into a thrown CAXException.
"""
import pathlib
import sys

ROOT = pathlib.Path(".")


def patch(rel, edits, append=None):
    path = ROOT / rel
    src = path.read_text()
    for old, new in edits:
        count = src.count(old)
        if count != 1:
            sys.exit(f"{rel}: anchor found {count} times (expected 1): {old[:80]!r}")
        src = src.replace(old, new)
    if append:
        src = src.rstrip("\n") + "\n" + append
    path.write_text(src)
    print(f"patched {rel}")


TRACE_MODULE = r'''
// ===== Host call trace (applesauce-jit-fix diagnostics) =====
pub mod host_call_trace {
    use std::sync::Mutex;

    const N: usize = 32;

    struct Ring {
        entries: [(&'static str, u32, u32); N],
        next: usize,
        filled: bool,
    }

    static RING: Mutex<Ring> = Mutex::new(Ring {
        entries: [("", 0, 0); N],
        next: 0,
        filled: false,
    });

    pub fn record(symbol: &'static str, svc_pc: u32, lr: u32) {
        if let Ok(mut ring) = RING.lock() {
            let i = ring.next;
            ring.entries[i] = (symbol, svc_pc, lr);
            ring.next = (i + 1) % N;
            if ring.next == 0 {
                ring.filled = true;
            }
        }
    }

    pub fn last() -> Option<&'static str> {
        let ring = RING.lock().ok()?;
        if !ring.filled && ring.next == 0 {
            return None;
        }
        Some(ring.entries[(ring.next + N - 1) % N].0)
    }

    pub fn dump() -> String {
        let Ok(ring) = RING.lock() else {
            return String::new();
        };
        let (start, count) = if ring.filled {
            (ring.next, N)
        } else {
            (0, ring.next)
        };
        let mut out = String::new();
        for k in 0..count {
            let (symbol, svc_pc, lr) = ring.entries[(start + k) % N];
            out.push_str(&format!(
                "\n    {} (svc pc={:#x}, guest lr={:#x})",
                symbol, svc_pc, lr
            ));
        }
        out
    }
}
'''

patch("src/dyld.rs", [
    (
        '                log_dbg!("Call to host function, already linked: {}", symbol);\n',
        '                log_dbg!("Call to host function, already linked: {}", symbol);\n'
        "                host_call_trace::record(symbol, svc_pc, cpu.regs()[crate::cpu::Cpu::LR]);\n",
    ),
    (
        "        let symbol = info.indirect_undef_symbols[idx].as_deref().unwrap();\n",
        "        let symbol = info.indirect_undef_symbols[idx].as_deref().unwrap();\n"
        "        host_call_trace::record(\n"
        "            Box::leak(symbol.to_string().into_boxed_str()),\n"
        "            svc_pc,\n"
        "            cpu.regs()[crate::cpu::Cpu::LR],\n"
        "        );\n",
    ),
], append=TRACE_MODULE)

patch("src/mem.rs", [(
    "            MAX_UNIQUE_LOGS\n        );\n    }\n\n    /// Special version of [Self::bytes_at]",
    "            MAX_UNIQUE_LOGS\n        );\n"
    "        if set.len() <= 3 {\n"
    "            log!(\n"
    '                "touchHLE::mem: recent host calls before this access:{}",\n'
    "                crate::dyld::host_call_trace::dump()\n"
    "            );\n"
    "        }\n"
    "    }\n\n    /// Special version of [Self::bytes_at]",
)])

patch("src/environment.rs", [
    (
        "                        count,\n                        BYPASS_LIMIT\n                    );\n                }",
        "                        count,\n                        BYPASS_LIMIT\n                    );\n                }\n"
        "                if count == 1 {\n"
        "                    let regs = *self.cpu.regs();\n"
        "                    log_no_panic!(\n"
        '                        "UDF bypass context: r0={:#x} r1={:#x} r2={:#x} r3={:#x} r4={:#x} r5={:#x} r6={:#x} r7={:#x} r8={:#x} r9={:#x} r10={:#x} r11={:#x} r12={:#x} sp={:#x}; recent host calls:{}",\n'
        "                        regs[0], regs[1], regs[2], regs[3], regs[4], regs[5], regs[6], regs[7],\n"
        "                        regs[8], regs[9], regs[10], regs[11], regs[12], regs[13],\n"
        "                        crate::dyld::host_call_trace::dump()\n"
        "                    );\n"
        "                }",
    ),
    (
        "                            f.call_from_guest(self);\n",
        "                            f.call_from_guest(self);\n"
        "                            if let Some(symbol) = crate::dyld::host_call_trace::last() {\n"
        '                                if (symbol.starts_with("_Audio") || symbol.starts_with("_ExtAudio"))\n'
        '                                    && !symbol.contains("PlaySystemSound")\n'
        "                                {\n"
        "                                    let status = self.cpu.regs()[0];\n"
        "                                    if status != 0 {\n"
        "                                        static LOGGED: std::sync::atomic::AtomicU32 =\n"
        "                                            std::sync::atomic::AtomicU32::new(0);\n"
        "                                        if LOGGED.fetch_add(1, std::sync::atomic::Ordering::Relaxed) < 300 {\n"
        "                                            log_no_panic!(\n"
        '                                                "Audio host call {} returned OSStatus {} ({})",\n'
        "                                                symbol,\n"
        "                                                status as i32,\n"
        "                                                crate::frameworks::core_audio_types::debug_fourcc(status)\n"
        "                                            );\n"
        "                                        }\n"
        "                                    }\n"
        "                                }\n"
        "                            }\n",
    ),
])

print("diagnostics patch applied")
