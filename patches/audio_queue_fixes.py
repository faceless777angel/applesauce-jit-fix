#!/usr/bin/env python3
"""AudioQueue fixes for the HyperHLE core.

Run from the root of an Applesauce checkout (tag ios-v0.4.1).

AudioQueueAllocateBufferWithPacketDescriptions ignored inNumberPacketDescriptions
and returned a buffer whose mPacketDescriptions was NULL (capacity 0). Talking
John fills mPacketDescriptions itself after AudioFileReadPackets (16-byte
AudioStreamPacketDescription each), so every sound effect (donut, pill, needle)
wrote straight into the NULL page and the game then hung or crashed. The buffer
now gets a real array of the requested size, freed with the buffer.
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


patch("src/frameworks/audio_toolbox/audio_queue.rs", [
    (
        "    _in_number_packet_desc: GuestUSize,\n"
        "    out_buffer: MutPtr<AudioQueueBufferRef>,\n"
        ") -> OSStatus {\n"
        "    AudioQueueAllocateBuffer(env, in_aq, in_buffer_byte_size, out_buffer)\n"
        "}",
        "    in_number_packet_desc: GuestUSize,\n"
        "    out_buffer: MutPtr<AudioQueueBufferRef>,\n"
        ") -> OSStatus {\n"
        "    let status = AudioQueueAllocateBuffer(env, in_aq, in_buffer_byte_size, out_buffer);\n"
        "    if status != 0 || in_number_packet_desc == 0 || out_buffer.is_null() {\n"
        "        return status;\n"
        "    }\n"
        "    let buffer_ref = env.mem.read(out_buffer);\n"
        "    let allocated_here = State::get(&mut env.framework_state)\n"
        "        .audio_queues\n"
        "        .get(&in_aq)\n"
        "        .is_some_and(|host_object| host_object.buffers.contains(&buffer_ref));\n"
        "    if !allocated_here {\n"
        "        return status;\n"
        "    }\n"
        "    // AudioStreamPacketDescription is 16 bytes.\n"
        "    let count = in_number_packet_desc.min(1 << 20);\n"
        "    let descriptions = env.mem.alloc(count * 16);\n"
        "    let mut buffer = env.mem.read(buffer_ref);\n"
        "    buffer.packet_description_capacity = count;\n"
        "    buffer._packet_descriptions = descriptions;\n"
        "    buffer._packet_description_count = 0;\n"
        "    env.mem.write(buffer_ref, buffer);\n"
        "    status\n"
        "}",
    ),
    (
        "        let buffer = env.mem.read(in_buffer);\n"
        "        env.mem.free(buffer.audio_data);\n",
        "        let buffer = env.mem.read(in_buffer);\n"
        "        env.mem.free(buffer.audio_data);\n"
        "        let descriptions = buffer._packet_descriptions;\n"
        "        if !descriptions.is_null() {\n"
        "            env.mem.free(descriptions);\n"
        "        }\n",
    ),
    (
        "        let buffer = env.mem.read(buffer_ptr);\n"
        "        env.mem.free(buffer.audio_data);\n",
        "        let buffer = env.mem.read(buffer_ptr);\n"
        "        env.mem.free(buffer.audio_data);\n"
        "        let descriptions = buffer._packet_descriptions;\n"
        "        if !descriptions.is_null() {\n"
        "            env.mem.free(descriptions);\n"
        "        }\n",
    ),
])

print("audio queue fixes applied")
