#!/usr/bin/env python3
"""Microphone input for the HyperHLE core and the Applesauce iOS host.

Run from the root of an Applesauce checkout (tag ios-v0.4.1).

Before: AudioQueueNewInput was a stub that never produced data, and
kAudioSessionProperty_AudioInputAvailable was hard-wired to 0, so games that
need a microphone (Talking John and friends) stop at "connect a microphone".

After:
  * the audio session reports a 1-channel input as available;
  * input queues record from the iPhone microphone through OpenAL Soft's
    capture API (resampled to the queue's rate) and hand filled buffers to the
    guest's AudioQueueInputCallback from the run loop, like Core Audio does;
  * level metering (kAudioQueueProperty_CurrentLevelMeter / ...DB) works on
    input queues;
  * the iOS host switches the AVAudioSession to PlayAndRecord (output stays on
    the loudspeaker) and asks for microphone permission the first time a game
    opens an input queue; Info.plist gets NSMicrophoneUsageDescription.
"""
import pathlib
import sys

ROOT = pathlib.Path(".")


def patch(rel, edits):
    path = ROOT / rel
    src = path.read_text()
    for old, new in edits:
        count = src.count(old)
        if count != 1:
            sys.exit(f"{rel}: anchor found {count} times (expected 1): {old[:80]!r}")
        src = src.replace(old, new)
    path.write_text(src)
    print(f"patched {rel}")


# --- OpenAL capture bindings ------------------------------------------------

patch("src/audio/openal_soft_wrapper/lib.rs", [(
    "    pub fn alcGetEnumValue(device: *mut ALCdevice, enumName: *const ALCchar) -> ALCenum;\n}",
    "    pub fn alcGetEnumValue(device: *mut ALCdevice, enumName: *const ALCchar) -> ALCenum;\n"
    "\n"
    "    pub fn alcCaptureOpenDevice(\n"
    "        devicename: *const ALCchar,\n"
    "        frequency: ALCuint,\n"
    "        format: ALCenum,\n"
    "        buffersize: ALCsizei,\n"
    "    ) -> *mut ALCdevice;\n"
    "    pub fn alcCaptureCloseDevice(device: *mut ALCdevice) -> ALCboolean;\n"
    "    pub fn alcCaptureStart(device: *mut ALCdevice);\n"
    "    pub fn alcCaptureStop(device: *mut ALCdevice);\n"
    "    pub fn alcCaptureSamples(device: *mut ALCdevice, buffer: *mut ALCvoid, samples: ALCsizei);\n"
    "}",
)])

# --- Audio session: a microphone is present ----------------------------------

patch("src/frameworks/audio_toolbox/audio_session.rs", [
    (
        "        kAudioSessionProperty_CurrentHardwareInputNumberChannels => {\n"
        "            env.mem.write(out_data.cast::<u32>(), 0);\n"
        "        }",
        "        kAudioSessionProperty_CurrentHardwareInputNumberChannels => {\n"
        "            // The built-in microphone (see audio_queue.rs input queues).\n"
        "            env.mem.write(out_data.cast::<u32>(), 1);\n"
        "        }",
    ),
    (
        "            // touchHLE currently does not emulate microphone input.\n"
        "            let value: u32 = 0;",
        "            // Input queues record from the host microphone.\n"
        "            let value: u32 = 1;",
    ),
])

# --- Audio queue: real input queues ------------------------------------------

MIC_CODE = r'''
// ===== Microphone input (applesauce-jit-fix) =====
//
// Input queues record from the host microphone through OpenAL Soft's capture
// API. Each run loop pass, as soon as enough captured frames exist to fill the
// buffer at the head of the queue, it is filled in the queue's format, taken
// off the queue and handed to the guest's AudioQueueInputCallback, matching
// Audio Queue Services. If no capture device can be opened the queue still
// runs in real time and delivers silence, so games don't stall.

use touchHLE_openal_soft_wrapper as mic_al;

const MIC_ALC_CAPTURE_SAMPLES: i32 = 0x312;
const kAudioQueueProperty_CurrentLevelMeterIn: AudioQueuePropertyID = fourcc(b"aqmv");
const kAudioQueueProperty_CurrentLevelMeterDBIn: AudioQueuePropertyID = fourcc(b"aqmd");

#[derive(Default)]
pub struct MicState {
    /// `*mut ALCdevice`, 0 if none.
    device: usize,
    rate: u32,
    started_at: Option<std::time::Instant>,
    frames_delivered: u64,
    /// Guest `AudioTimeStamp` passed to the callback, 0 until allocated.
    timestamp: u32,
    level_avg: f32,
    level_peak: f32,
    metering: u32,
}

fn is_input_queue(env: &mut Environment, in_aq: AudioQueueRef) -> bool {
    State::get(&mut env.framework_state)
        .audio_queues
        .get(&in_aq)
        .is_some_and(|host_object| host_object.is_input)
}

/// The iOS host app exports this to switch the AVAudioSession to
/// PlayAndRecord and ask for microphone permission.
#[cfg(target_os = "ios")]
fn request_host_microphone() {
    extern "C" {
        fn dlsym(
            handle: *mut std::ffi::c_void,
            symbol: *const std::ffi::c_char,
        ) -> *mut std::ffi::c_void;
    }
    let rtld_default = -2isize as *mut std::ffi::c_void;
    let symbol = unsafe {
        dlsym(
            rtld_default,
            b"touchhle_ios_enable_microphone\0".as_ptr() as *const std::ffi::c_char,
        )
    };
    if symbol.is_null() {
        log!("Warning: the host app has no touchhle_ios_enable_microphone; recording may be silent.");
        return;
    }
    let enable: extern "C" fn() = unsafe { std::mem::transmute(symbol) };
    enable();
}

#[cfg(not(target_os = "ios"))]
fn request_host_microphone() {}

fn mic_open(env: &mut Environment, in_aq: AudioQueueRef) {
    let state = State::get(&mut env.framework_state);
    let Some(host_object) = state.audio_queues.get(&in_aq) else {
        return;
    };
    let format = host_object.format;
    let sample_rate = format.sample_rate;
    let mic = state.mics.entry(in_aq).or_default();
    if mic.device != 0 {
        return;
    }
    let rate = if (4000.0..=192000.0).contains(&sample_rate) {
        sample_rate as u32
    } else {
        44100
    };
    mic.rate = rate;

    request_host_microphone();

    let device = unsafe {
        mic_al::alcCaptureOpenDevice(
            std::ptr::null(),
            rate,
            al::AL_FORMAT_MONO16,
            (rate * 2) as i32,
        )
    };
    if device.is_null() {
        log!(
            "Warning: alcCaptureOpenDevice({} Hz) failed; microphone queue {:?} will deliver silence.",
            rate,
            in_aq
        );
    } else {
        log!("Microphone capture opened at {} Hz for input queue {:?}.", rate, in_aq);
    }
    mic.device = device as usize;
}

fn mic_close(env: &mut Environment, in_aq: AudioQueueRef) {
    let Some(mic) = State::get(&mut env.framework_state).mics.remove(&in_aq) else {
        return;
    };
    if mic.device != 0 {
        unsafe {
            mic_al::alcCaptureStop(mic.device as *mut _);
            mic_al::alcCaptureCloseDevice(mic.device as *mut _);
        }
    }
    if mic.timestamp != 0 {
        env.mem.free(Ptr::from_bits(mic.timestamp));
    }
}

fn mic_start(env: &mut Environment, in_aq: AudioQueueRef) -> OSStatus {
    mic_open(env, in_aq);
    let state = State::get(&mut env.framework_state);
    if let Some(mic) = state.mics.get_mut(&in_aq) {
        if mic.device != 0 {
            unsafe { mic_al::alcCaptureStart(mic.device as *mut _) };
        }
        mic.started_at = Some(std::time::Instant::now());
        mic.frames_delivered = 0;
    }
    if let Some(host_object) = state.audio_queues.get_mut(&in_aq) {
        host_object.is_running = AudioQueueIsRunning::Running;
    }
    log!("AudioQueueStart: microphone input queue {:?} is recording.", in_aq);
    notify_aq_is_running(env, in_aq);
    0
}

fn mic_pause(env: &mut Environment, in_aq: AudioQueueRef) -> OSStatus {
    let state = State::get(&mut env.framework_state);
    if let Some(mic) = state.mics.get_mut(&in_aq) {
        if mic.device != 0 {
            unsafe { mic_al::alcCaptureStop(mic.device as *mut _) };
        }
        mic.started_at = None;
    }
    if let Some(host_object) = state.audio_queues.get_mut(&in_aq) {
        host_object.is_running = AudioQueueIsRunning::Stopped;
    }
    0
}

fn mic_stop(env: &mut Environment, in_aq: AudioQueueRef) -> OSStatus {
    mic_pause(env, in_aq);
    finish_stopping_audio_queue(env, in_aq);
    0
}

fn mic_handle(env: &mut Environment, in_aq: AudioQueueRef) {
    use crate::frameworks::core_audio_types::{AudioTimeStamp, SMPTETime};

    // A handful of buffers per run loop pass at most.
    for _ in 0..8 {
        let state = State::get(&mut env.framework_state);
        let Some(host_object) = state.audio_queues.get(&in_aq) else {
            return;
        };
        if host_object.is_running != AudioQueueIsRunning::Running {
            return;
        }
        let Some(&buffer_ref) = host_object.buffer_queue.front() else {
            return;
        };
        let format = host_object.format;
        let callback_proc = host_object.callback_proc;
        let callback_user_data = host_object.callback_user_data;
        let Some(mic) = state.mics.get_mut(&in_aq) else {
            return;
        };

        let format_id = format.format_id;
        let format_flags = format.format_flags;
        let channels = {
            let c = format.channels_per_frame;
            if c == 0 { 1 } else { c }
        };
        let bits = {
            let b = format.bits_per_channel;
            if b == 0 { 16 } else { b }
        };
        let bytes_per_frame = {
            let b = format.bytes_per_frame;
            if b == 0 { channels * bits / 8 } else { b }
        };
        if bytes_per_frame == 0 {
            return;
        }

        let mut buffer = env.mem.read(buffer_ref);
        let capacity = buffer.audio_data_bytes_capacity;
        let audio_data = buffer.audio_data;
        let frames = capacity / bytes_per_frame;
        if frames == 0 {
            return;
        }

        let rate = if mic.rate == 0 { 44100 } else { mic.rate };
        let available: u64 = if mic.device != 0 {
            let mut n: i32 = 0;
            unsafe {
                mic_al::alcGetIntegerv(mic.device as *mut _, MIC_ALC_CAPTURE_SAMPLES, 1, &mut n)
            };
            n.max(0) as u64
        } else {
            let Some(started_at) = mic.started_at else {
                return;
            };
            let due = (started_at.elapsed().as_secs_f64() * rate as f64) as u64;
            due.saturating_sub(mic.frames_delivered)
        };
        if available < frames as u64 {
            return;
        }

        let mut samples = vec![0i16; frames as usize];
        if mic.device != 0 {
            unsafe {
                mic_al::alcCaptureSamples(
                    mic.device as *mut _,
                    samples.as_mut_ptr() as *mut _,
                    frames as i32,
                )
            };
        }

        let mut sum = 0f64;
        let mut peak = 0f32;
        for &s in &samples {
            let v = s as f32 / 32768.0;
            sum += (v as f64) * (v as f64);
            peak = peak.max(v.abs());
        }
        mic.level_avg = (sum / frames as f64).sqrt() as f32;
        mic.level_peak = peak;

        let sample_time = mic.frames_delivered as f64;
        mic.frames_delivered += frames as u64;
        if mic.timestamp == 0 {
            mic.timestamp = env.mem.alloc(guest_size_of::<AudioTimeStamp>()).to_bits();
        }
        let timestamp_bits = mic.timestamp;

        // Convert to the queue's format.
        let is_lpcm = format_id == kAudioFormatLinearPCM;
        let is_float = format_flags & kAudioFormatFlagIsFloat != 0;
        let is_signed = format_flags & (1 << 2) != 0;
        let bytes_per_sample = (bytes_per_frame / channels).max(1) as usize;
        let mut out: Vec<u8> = Vec::with_capacity((frames * bytes_per_frame) as usize);
        for &s in &samples {
            for _ in 0..channels {
                if !is_lpcm {
                    out.extend(std::iter::repeat(0u8).take(bytes_per_sample));
                    continue;
                }
                match (is_float, bits) {
                    (true, 32) => out.extend_from_slice(&(s as f32 / 32768.0).to_le_bytes()),
                    (true, 64) => out.extend_from_slice(&(s as f64 / 32768.0).to_le_bytes()),
                    (false, 16) => out.extend_from_slice(&s.to_le_bytes()),
                    (false, 8) => out.push(if is_signed {
                        (s >> 8) as i8 as u8
                    } else {
                        ((s >> 8) as i32 + 128) as u8
                    }),
                    (false, 24) => out.extend_from_slice(&((s as i32) << 8).to_le_bytes()[..3]),
                    (false, 32) => out.extend_from_slice(&((s as i32) << 16).to_le_bytes()),
                    _ => out.extend(std::iter::repeat(0u8).take(bytes_per_sample)),
                }
            }
        }
        out.resize((frames * bytes_per_frame) as usize, 0);
        let byte_size = out.len() as u32;

        env.mem
            .bytes_at_mut(audio_data.cast(), byte_size)
            .copy_from_slice(&out);
        buffer.audio_data_byte_size = byte_size;
        env.mem.write(buffer_ref, buffer);

        let timestamp_ptr: MutPtr<AudioTimeStamp> = Ptr::from_bits(timestamp_bits);
        env.mem.write(
            timestamp_ptr,
            AudioTimeStamp {
                sample_time,
                host_time: 0,
                rate_scalar: 1.0,
                world_clock_type: 0,
                SMPTE_time: SMPTETime {
                    subframes: 0,
                    subframe_divisor: 0,
                    counter: 0,
                    type_: 0,
                    flags: 0,
                    hours: 0,
                    minutes: 0,
                    seconds: 0,
                    frames: 0,
                },
                // kAudioTimeStampSampleTimeValid
                flags: 1,
                _reserved: 0,
            },
        );

        if let Some(host_object) = State::get(&mut env.framework_state)
            .audio_queues
            .get_mut(&in_aq)
        {
            host_object.buffer_queue.pop_front();
        }

        let start_time: ConstVoidPtr = timestamp_ptr.cast_const().cast();
        let no_packet_descs: ConstVoidPtr = Ptr::null();
        let () = callback_proc.call_from_host(
            env,
            (
                callback_user_data,
                in_aq,
                buffer_ref,
                start_time,
                frames,
                no_packet_descs,
            ),
        );
    }
}

fn mic_channels(env: &mut Environment, in_aq: AudioQueueRef) -> u32 {
    State::get(&mut env.framework_state)
        .audio_queues
        .get(&in_aq)
        .map(|host_object| {
            let format = host_object.format;
            let c = format.channels_per_frame;
            if c == 0 { 1 } else { c }
        })
        .unwrap_or(1)
}

fn mic_get_property_size(
    env: &mut Environment,
    in_aq: AudioQueueRef,
    in_property_id: AudioQueuePropertyID,
    out_data_size: MutPtr<u32>,
) -> Option<OSStatus> {
    if in_property_id != kAudioQueueProperty_CurrentLevelMeterIn
        && in_property_id != kAudioQueueProperty_CurrentLevelMeterDBIn
    {
        return None;
    }
    if !is_input_queue(env, in_aq) {
        return None;
    }
    let channels = mic_channels(env, in_aq);
    if !out_data_size.is_null() {
        env.mem.write(out_data_size, channels * 8);
    }
    Some(0)
}

fn mic_get_property(
    env: &mut Environment,
    in_aq: AudioQueueRef,
    in_property_id: AudioQueuePropertyID,
    out_property_data: MutVoidPtr,
    io_data_size: MutPtr<u32>,
) -> Option<OSStatus> {
    if in_property_id != kAudioQueueProperty_CurrentLevelMeterIn
        && in_property_id != kAudioQueueProperty_CurrentLevelMeterDBIn
        && in_property_id != kAudioQueueProperty_EnableLevelMetering
    {
        return None;
    }
    if !is_input_queue(env, in_aq) || out_property_data.is_null() {
        return None;
    }
    let channels = mic_channels(env, in_aq);
    let mic = State::get(&mut env.framework_state)
        .mics
        .entry(in_aq)
        .or_default();
    let metering = mic.metering;
    let (level_avg, level_peak) = (mic.level_avg, mic.level_peak);

    if in_property_id == kAudioQueueProperty_EnableLevelMetering {
        env.mem.write(out_property_data.cast::<u32>(), metering);
        if !io_data_size.is_null() {
            env.mem.write(io_data_size, 4);
        }
        return Some(0);
    }

    let (avg, peak) = if in_property_id == kAudioQueueProperty_CurrentLevelMeterDBIn {
        let db = |v: f32| -> f32 {
            if v <= 0.000_001 {
                -120.0
            } else {
                (20.0 * v.log10()).max(-120.0)
            }
        };
        (db(level_avg), db(level_peak))
    } else {
        (level_avg, level_peak)
    };
    let provided = if io_data_size.is_null() {
        8
    } else {
        env.mem.read(io_data_size)
    };
    let count = (provided / 8).min(channels);
    for i in 0..count {
        let level: MutPtr<f32> = Ptr::from_bits(out_property_data.to_bits() + i * 8);
        env.mem.write(level, avg);
        env.mem.write(level + 1, peak);
    }
    if !io_data_size.is_null() {
        env.mem.write(io_data_size, count * 8);
    }
    Some(0)
}

pub const FUNCTIONS: FunctionExports = &['''

patch("src/frameworks/audio_toolbox/audio_queue.rs", [
    # State: per-queue microphone state.
    (
        "    audio_queues: HashMap<AudioQueueRef, AudioQueueHostObject>,\n}",
        "    audio_queues: HashMap<AudioQueueRef, AudioQueueHostObject>,\n"
        "    mics: HashMap<AudioQueueRef, MicState>,\n}",
    ),
    # NewInput: no longer a stub.
    (
        '    log!("TODO: AudioQueueNewInput(...) stubbed");\n\n    assert!(in_flags == 0);\n',
        '    if in_flags != 0 {\n'
        '        log!("AudioQueueNewInput: ignoring flags {:#x}", in_flags);\n'
        '    }\n',
    ),
    (
        "    ns_run_loop::add_audio_queue(env, in_callback_run_loop, aq_ref);\n\n    0\n}\n\npub const FUNCTIONS: FunctionExports = &[",
        "    if let Some(host_object) = State::get(&mut env.framework_state)\n"
        "        .audio_queues\n"
        "        .get_mut(&aq_ref)\n"
        "    {\n"
        "        host_object.is_input = true;\n"
        "        log!(\"AudioQueueNewInput: {:?} records {:?}\", aq_ref, host_object.format);\n"
        "    }\n"
        "\n"
        "    ns_run_loop::add_audio_queue(env, in_callback_run_loop, aq_ref);\n\n    0\n}\n"
        + MIC_CODE,
    ),
    # Input queues never touch the OpenAL playback path.
    (
        "fn prime_audio_queue(env: &mut Environment, in_aq: AudioQueueRef) {\n",
        "fn prime_audio_queue(env: &mut Environment, in_aq: AudioQueueRef) {\n"
        "    if is_input_queue(env, in_aq) {\n"
        "        return;\n"
        "    }\n",
    ),
    (
        "pub fn handle_audio_queue(env: &mut Environment, in_aq: AudioQueueRef) {\n",
        "pub fn handle_audio_queue(env: &mut Environment, in_aq: AudioQueueRef) {\n"
        "    if is_input_queue(env, in_aq) {\n"
        "        mic_handle(env, in_aq);\n"
        "        return;\n"
        "    }\n",
    ),
    (
        "    _in_device_start_time: ConstVoidPtr,\n) -> OSStatus {\n    return_if_null!(in_aq);\n",
        "    _in_device_start_time: ConstVoidPtr,\n) -> OSStatus {\n    return_if_null!(in_aq);\n"
        "    if is_input_queue(env, in_aq) {\n"
        "        return mic_start(env, in_aq);\n"
        "    }\n",
    ),
    (
        "pub fn AudioQueuePause(env: &mut Environment, in_aq: AudioQueueRef) -> OSStatus {\n    return_if_null!(in_aq);\n",
        "pub fn AudioQueuePause(env: &mut Environment, in_aq: AudioQueueRef) -> OSStatus {\n    return_if_null!(in_aq);\n"
        "    if is_input_queue(env, in_aq) {\n"
        "        return mic_pause(env, in_aq);\n"
        "    }\n",
    ),
    (
        "pub fn AudioQueueStop(env: &mut Environment, in_aq: AudioQueueRef, in_immediate: bool) -> OSStatus {\n    return_if_null!(in_aq);\n",
        "pub fn AudioQueueStop(env: &mut Environment, in_aq: AudioQueueRef, in_immediate: bool) -> OSStatus {\n    return_if_null!(in_aq);\n"
        "    if is_input_queue(env, in_aq) {\n"
        "        return mic_stop(env, in_aq);\n"
        "    }\n",
    ),
    (
        "    _in_immediate: bool,\n) -> OSStatus {\n    return_if_null!(in_aq);\n",
        "    _in_immediate: bool,\n) -> OSStatus {\n    return_if_null!(in_aq);\n"
        "    mic_close(env, in_aq);\n",
    ),
    # kAudioQueueProperty_StreamDescription ('aqft') was advertised by
    # property_size() but never answered, so AudioQueueGetProperty returned
    # kAudioQueueErr_InvalidProperty. Talking John wraps that call in
    # XThrowIfError and aborts with an uncaught CAXException.
    (
        "        kAudioQueueProperty_HardwareCodecPolicy => {\n"
        "            env.mem.write(\n"
        "                out_property_data.cast::<u32>(),\n",
        "        kAudioQueueProperty_StreamDescription => {\n"
        "            let format = host_object.format;\n"
        "            env.mem.write(\n"
        "                out_property_data.cast::<AudioStreamBasicDescription>(),\n"
        "                format,\n"
        "            );\n"
        "            env.mem.write(\n"
        "                io_data_size,\n"
        "                guest_size_of::<AudioStreamBasicDescription>(),\n"
        "            );\n"
        "        }\n"
        "        kAudioQueueProperty_HardwareCodecPolicy => {\n"
        "            env.mem.write(\n"
        "                out_property_data.cast::<u32>(),\n",
    ),
    # Level metering on input queues.
    (
        "    out_data_size: MutPtr<u32>,\n) -> OSStatus {\n    return_if_null!(in_aq);\n\n    match property_size(in_property_id) {",
        "    out_data_size: MutPtr<u32>,\n) -> OSStatus {\n    return_if_null!(in_aq);\n"
        "    if let Some(status) = mic_get_property_size(env, in_aq, in_property_id, out_data_size) {\n"
        "        return status;\n"
        "    }\n"
        "\n    match property_size(in_property_id) {",
    ),
    (
        "    io_data_size: MutPtr<u32>,\n) -> OSStatus {\n    return_if_null!(in_aq);\n",
        "    io_data_size: MutPtr<u32>,\n) -> OSStatus {\n    return_if_null!(in_aq);\n"
        "    if let Some(status) =\n"
        "        mic_get_property(env, in_aq, in_property_id, out_property_data, io_data_size)\n"
        "    {\n"
        "        return status;\n"
        "    }\n",
    ),
    (
        "    in_data_size: u32,\n) -> OSStatus {\n    return_if_null!(in_aq);\n",
        "    in_data_size: u32,\n) -> OSStatus {\n    return_if_null!(in_aq);\n"
        "    if in_property_id == kAudioQueueProperty_EnableLevelMetering\n"
        "        && !in_property_data.is_null()\n"
        "        && is_input_queue(env, in_aq)\n"
        "    {\n"
        "        let value = env.mem.read(in_property_data.cast::<u32>());\n"
        "        State::get(&mut env.framework_state)\n"
        "            .mics\n"
        "            .entry(in_aq)\n"
        "            .or_default()\n"
        "            .metering = value;\n"
        "        return 0;\n"
        "    }\n",
    ),
])

# --- iOS host: audio session + permission ------------------------------------

HOST_CODE = r'''

// Called by the emulator core (looked up with dlsym) the first time a game
// opens a recording audio queue. PlayAndRecord lets OpenAL Soft capture from
// the microphone; DefaultToSpeaker keeps game audio on the loudspeaker rather
// than the earpiece.
__attribute__((used, visibility("default")))
void touchhle_ios_enable_microphone(void) {
    void (^apply)(void) = ^{
        AVAudioSession *session = [AVAudioSession sharedInstance];
        NSError *error = nil;
        AVAudioSessionCategoryOptions options =
            AVAudioSessionCategoryOptionDefaultToSpeaker
            | AVAudioSessionCategoryOptionAllowBluetooth
            | AVAudioSessionCategoryOptionMixWithOthers;
        if (![session setCategory:AVAudioSessionCategoryPlayAndRecord
                      withOptions:options
                            error:&error]) {
            fprintf(stderr, "touchHLE mic: setCategory failed: %s\n",
                    error.localizedDescription.UTF8String);
        }
        error = nil;
        if (![session setActive:YES error:&error]) {
            fprintf(stderr, "touchHLE mic: setActive failed: %s\n",
                    error.localizedDescription.UTF8String);
        }
        [session requestRecordPermission:^(BOOL granted) {
            fprintf(stderr, "touchHLE mic: record permission %s\n",
                    granted ? "granted" : "denied");
        }];
        fprintf(stderr, "touchHLE mic: audio session set to PlayAndRecord\n");
    };
    if ([NSThread isMainThread]) {
        apply();
    } else {
        dispatch_sync(dispatch_get_main_queue(), apply);
    }
}
'''

main_m = ROOT / "platform/ios/Sources/main.m"
src = main_m.read_text()
anchor = "#import <Foundation/Foundation.h>\n"
if src.count(anchor) != 1:
    sys.exit("main.m: Foundation import anchor not found exactly once")
src = src.replace(anchor, "#import <AVFoundation/AVFoundation.h>\n" + anchor)
src = src.rstrip("\n") + "\n" + HOST_CODE
main_m.write_text(src)
print("patched platform/ios/Sources/main.m")

patch("platform/ios/Config/Info.plist", [(
    "\t<key>UIFileSharingEnabled</key>",
    "\t<key>NSMicrophoneUsageDescription</key>\n"
    "\t<string>Games running in Applesauce can use the microphone, for example to repeat what you say.</string>\n"
    "\t<key>UIFileSharingEnabled</key>",
)])

print("microphone patch applied")
