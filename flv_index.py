# -*- coding: utf-8 -*-
"""给无损合并出来的 FLV 注入 onMetaData.keyframes 索引。

ffmpeg 的 FLV muxer 默认不写 keyframes 索引，mpegts.js 的 isSeekable() 因此为
false，拖动进度条走 unbuffered seek 时直接 return，表现为一直卡在加载。
本模块扫描 FLV 的所有视频关键帧，重写首个 script tag 的 onMetaData，插入
keyframes.times（秒）与 keyframes.filepositions（tag 起始字节偏移），从而让
播放器能按字节范围跳转。全程不触碰音视频 tag 本身，画质零损失。

可单独运行：python flv_index.py <input.flv> [-o output.flv]
"""

import os
import struct


def _u24(buf, pos):
    # FLV tag 头里的 data size / timestamp 都是大端 24 位
    return (buf[pos] << 16) | (buf[pos + 1] << 8) | buf[pos + 2]


def _skip_amf(buf, pos):
    """跳过一个 AMF0 值，返回结束位置。"""
    if pos >= len(buf):
        return pos
    t = buf[pos]
    pos += 1
    if t == 0x00:      # number
        return pos + 8
    if t == 0x01:      # boolean
        return pos + 1
    if t == 0x02:      # string
        n = struct.unpack(">H", buf[pos:pos + 2])[0]
        return pos + 2 + n
    if t == 0x03:      # object
        return _skip_amf_object(buf, pos)
    if t in (0x05, 0x06, 0x0D):  # null / undefined / unsupported
        return pos
    if t == 0x07:      # reference
        return pos + 2
    if t == 0x08:      # ECMA array
        pos += 4
        return _skip_amf_object(buf, pos)
    if t == 0x0A:      # strict array
        n = struct.unpack(">I", buf[pos:pos + 4])[0]
        pos += 4
        for _ in range(n):
            pos = _skip_amf(buf, pos)
        return pos
    if t == 0x0B:      # date
        return pos + 10
    if t in (0x0C, 0x0F):  # long string / xml
        n = struct.unpack(">I", buf[pos:pos + 4])[0]
        return pos + 4 + n
    if t == 0x10:      # typed object
        n = struct.unpack(">H", buf[pos:pos + 2])[0]
        return _skip_amf_object(buf, pos + 2 + n)
    if t == 0x11:      # AVMplus reference
        return pos + 1
    return pos


def _skip_amf_object(buf, pos):
    """跳过一串 AMF0 object 属性（到 00 00 09 结束标记之后）。"""
    while pos + 3 <= len(buf):
        if buf[pos:pos + 3] == b"\x00\x00\x09":
            return pos + 3
        n = struct.unpack(">H", buf[pos:pos + 2])[0]
        pos += 2 + n
        if pos > len(buf):
            break
        pos = _skip_amf(buf, pos)
    return pos


def _parse_props(buf, start):
    """解析从 start（0x08 之后的 4 字节计数已跳过）开始的 ECMA array 属性。

    返回 (props, end)，props 为 [(key_bytes, value_bytes), ...]，end 指向结束标记之后。
    """
    n = struct.unpack(">I", buf[start:start + 4])[0]
    pos = start + 4
    props = []
    for _ in range(n):
        if pos + 2 > len(buf):
            break
        klen = struct.unpack(">H", buf[pos:pos + 2])[0]
        pos += 2
        key = buf[pos:pos + klen]
        pos += klen
        vstart = pos
        pos = _skip_amf(buf, pos)
        props.append((key, buf[vstart:pos]))
    # 结束标记 00 00 09
    if buf[pos:pos + 3] == b"\x00\x00\x09":
        pos += 3
    return props, pos


def _amf_str(s):
    b = s.encode("utf-8") if isinstance(s, str) else s
    return b"\x02" + struct.pack(">H", len(b)) + b


def _amf_num(x):
    return b"\x00" + struct.pack(">d", float(x))


def _amf_strict_array(items):
    return b"\x0a" + struct.pack(">I", len(items)) + b"".join(items)


def _amf_object(fields):
    """fields: [(key_bytes, value_bytes), ...]"""
    out = b"\x03"
    for k, v in fields:
        out += struct.pack(">H", len(k)) + k + v
    return out + b"\x00\x00\x09"


def parse_tags(data):
    """返回 (tags, first_script_index)。tag = dict(type, timestamp, data, offset)。"""
    tags = []
    pos = 9  # FLV header
    if len(data) < 13:
        return tags, -1
    prev_size = struct.unpack(">I", data[9:13])[0]  # PreviousTagSize0
    pos = 13
    first_script = -1
    while pos + 11 <= len(data):
        tag_type = data[pos]
        data_size = _u24(data, pos + 1)
        timestamp = _u24(data, pos + 4) | (data[pos + 7] << 24)
        body_start = pos + 11
        body_end = body_start + data_size
        if body_end + 4 > len(data):
            break
        body = data[body_start:body_end]
        tags.append({"type": tag_type, "ts": timestamp,
                     "data": body, "offset": pos})
        if tag_type == 18 and first_script < 0:
            first_script = len(tags) - 1
        pos = body_end + 4  # + PreviousTagSize
        prev_size = data_size
    return tags, first_script


def collect_keyframes(tags):
    """返回关键帧列表 [(秒, 原文件 tag 偏移)]。

    关键帧判定与 mpegts.js 一致：FrameType==1，或 AVC NALU 里出现 IDR(type 5)。
    排除 AVC sequence header(AVCPacketType==0)。
    """
    out = []
    for t in tags:
        if t["type"] != 9:  # video
            continue
        d = t["data"]
        if len(d) < 2:
            continue
        frame_type = (d[0] >> 4) & 0x0F
        codec = d[0] & 0x0F
        is_key = frame_type == 1
        if codec == 7:  # AVC
            pkt_type = d[1]
            if pkt_type == 0:  # sequence header，不算关键帧
                continue
            if pkt_type == 1:  # NALU：扫描是否含 IDR
                pos = 2
                nalu_len_size = 4
                # 从 AVC vidc 的 lengthSizeMinusOne 推；默认 4，兜底 4
                while pos + nalu_len_size <= len(d):
                    if nalu_len_size == 4:
                        nlen = struct.unpack(">I", d[pos:pos + 4])[0]
                    else:
                        nlen = d[pos]
                    if nlen <= 0 or pos + nalu_len_size + nlen > len(d):
                        break
                    unit = d[pos + nalu_len_size] & 0x1F
                    if unit == 5:
                        is_key = True
                        break
                    pos += nalu_len_size + nlen
                if not is_key:
                    continue
        elif codec != 7 and not is_key:
            continue
        if is_key:
            out.append((t["ts"] / 1000.0, t["offset"]))
    return out


def build_script_tag(original_body, keyframes, delta):
    """重写 onMetaData，插入 keyframes；返回新的 script tag data。

    delta = 新 script tag 相对原 script tag 的字节增量，用于把所有关键帧的
    fileposition 平移到新文件的绝对偏移。
    """
    # original_body: 00.. = "onMetaData" AMF string，紧接 ECMA array
    name_len = struct.unpack(">H", original_body[1:3])[0]
    p = 3 + name_len
    if p >= len(original_body) or original_body[p] != 0x08:
        raise ValueError("onMetaData 不是 ECMA array")
    props, _ = _parse_props(original_body, p + 1)
    props = [(k, v) for k, v in props if k != b"keyframes"]
    times = [_amf_num(sec) for sec, _ in keyframes]
    positions = [_amf_num(float(off + delta)) for _, off in keyframes]
    kf_value = _amf_object([
        (b"times", _amf_strict_array(times)),
        (b"filepositions", _amf_strict_array(positions)),
    ])
    props.append((b"keyframes", kf_value))
    ecma = b"\x08" + struct.pack(">I", len(props)) + b"".join(
        struct.pack(">H", len(k)) + k + v for k, v in props) + b"\x00\x00\x09"
    return original_body[:3 + name_len] + ecma


def inject(src, dst=None):
    """给 src 注入 keyframes 索引写到 dst（默认 src+.indexed.flv）。返回统计信息。"""
    with open(src, "rb") as f:
        data = f.read()
    tags, si = parse_tags(data)
    if si < 0:
        raise ValueError("找不到 onMetaData script tag")
    kfs = collect_keyframes(tags)
    if len(kfs) < 2:
        raise ValueError("关键帧太少(%d)，无法建索引" % len(kfs))

    old_body = tags[si]["data"]
    # 先构造一次拿长度；delta 只影响 fileposition 数值（AMF number 恒 8 字节），
    # 故新 script tag 长度与 delta 无关，可一次算出。
    probe = build_script_tag(old_body, kfs, 0)
    delta = len(probe) - len(old_body)
    new_body = build_script_tag(old_body, kfs, delta)
    assert len(new_body) == len(probe)

    out = bytearray(data[:13])  # header + PreviousTagSize0
    for i, t in enumerate(tags):
        body = new_body if i == si else t["data"]
        out += bytes([t["type"]]) + len(body).to_bytes(3, "big")
        # timestamp 低 3 字节 + ext，streamid 保持原样：从原 tag header 复制
        hdr = data[t["offset"]:t["offset"] + 11]
        out += hdr[4:11]
        out += body
        out += struct.pack(">I", len(body))
    target = dst or (src + ".indexed.flv")
    tmp = target + ".tmp"
    with open(tmp, "wb") as f:
        f.write(out)
    os.replace(tmp, target)
    return {
        "keyframes": len(kfs),
        "first_kf_sec": round(kfs[0][0], 2),
        "last_kf_sec": round(kfs[-1][0], 2),
        "script_delta": delta,
        "in_bytes": len(data),
        "out_bytes": len(out),
    }


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("-o", "--output")
    a = ap.parse_args()
    info = inject(a.src, a.output)
    print("keyframes=%d  first=%.2fs  last=%.2fs  meta+%dB  %.1f->%.1fMiB" % (
        info["keyframes"], info["first_kf_sec"], info["last_kf_sec"],
        info["script_delta"], info["in_bytes"] / 1048576.0,
        info["out_bytes"] / 1048576.0))
