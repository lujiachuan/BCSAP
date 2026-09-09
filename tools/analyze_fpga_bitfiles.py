"""解析 NI LabVIEW FPGA .lvbitx 文件并输出中文摘要。"""

from __future__ import annotations

import argparse
import base64
import hashlib
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def get_text(node: ET.Element, path: str, default: str = "-") -> str:
    value = node.findtext(path)
    return value.strip() if value and value.strip() else default


def describe_type(container: ET.Element | None) -> str:
    if container is None:
        return "未知"
    data_type = container.find("./Datatype")
    if data_type is None:
        data_type = container.find("./DataType")
    if data_type is None or not list(data_type):
        return "未知"

    item = list(data_type)[0]
    kind = item.tag
    if kind == "FXP":
        signed = "有符号" if get_text(item, "Signed") == "true" else "无符号"
        return (
            f"FXP({signed}, 字长={get_text(item, 'WordLength')}, "
            f"整数字长={get_text(item, 'IntegerWordLength')})"
        )
    if kind == "Cluster":
        fields = [f"{get_text(field, 'Name')}:{field.tag}" for field in item.findall("./TypeList/*")]
        return f"Cluster({', '.join(fields)})"
    if kind == "Array":
        element = item.find("./Type/*")
        return f"Array({element.tag if element is not None else '未知'})"
    return kind


def describe_dma_type(channel: ET.Element) -> str:
    data_type = channel.find("DataType")
    if data_type is None:
        return "未知"
    subtype = get_text(data_type, "SubType")
    word_length = get_text(data_type, "WordLength")
    return f"{subtype} ({word_length} bit)" if word_length != "-" else subtype


def infer_purpose(root: ET.Element) -> list[str]:
    vi_name = get_text(root, "./VI/Name").lower()
    register_names = {
        get_text(register, "Name").lower()
        for register in root.findall("./VI/RegisterList/Register")
        if get_text(register, "Internal") != "true"
    }
    clues: list[str] = []
    if "lsb_offset" in vi_name:
        clues.append("从名称和 4 个 Cluster 输出判断：用于观察/标定 NI 9239 四通道的低位或零点偏移")
    else:
        clues.append("NI 9239 入门采集 VI：从模块采集数据，并通过 DMA FIFO 传给主机")
    if {"ave_num", "1/num", "avg"} <= register_names:
        clues.append("包含可配置平均功能（平均次数、倒数系数和 AVG 开关）")
    if "data output" in register_names:
        clues.append("额外暴露 Data output 指示器，便于主机直接读取一个数据输出值")
    if root.find(".//DmaChannelAllocationList/Channel") is None:
        clues.append("没有用户 DMA FIFO，数据主要通过前面板寄存器读取")
    return clues


def parse_bitfile(path: Path) -> dict[str, object]:
    root = ET.parse(path).getroot()
    bitstream_text = get_text(root, "Bitstream", "")
    expected_md5 = get_text(root, "BitstreamMD5")
    decoded_size: int | None = None
    actual_md5 = "-"
    md5_ok: bool | None = None
    if bitstream_text:
        bitstream = base64.b64decode(bitstream_text, validate=False)
        decoded_size = len(bitstream)
        actual_md5 = hashlib.md5(bitstream).hexdigest()  # noqa: S324 - 校验文件已有摘要
        md5_ok = actual_md5.lower() == expected_md5.lower()

    registers = []
    for register in root.findall("./VI/RegisterList/Register"):
        internal = get_text(register, "Internal") == "true"
        indicator = get_text(register, "Indicator") == "true"
        registers.append(
            {
                "name": get_text(register, "Name"),
                "direction": "FPGA → 主机（读）" if indicator else "主机 → FPGA（写）",
                "type": describe_type(register),
                "offset": get_text(register, "Offset"),
                "bits": get_text(register, "SizeInBits"),
                "internal": internal,
            }
        )

    dma_channels = []
    for channel in root.findall(".//DmaChannelAllocationList/Channel"):
        direction = get_text(channel, "Direction")
        direction_cn = {
            "TargetToHost": "FPGA → 主机",
            "HostToTarget": "主机 → FPGA",
        }.get(direction, direction)
        dma_channels.append(
            {
                "name": channel.get("name", "-"),
                "direction": direction_cn,
                "type": describe_dma_type(channel),
                "elements": get_text(channel, "NumberOfElements"),
            }
        )

    return {
        "path": path,
        "file_size": path.stat().st_size,
        "bitfile_version": get_text(root, "BitfileVersion"),
        "bitstream_version": get_text(root, "BitstreamVersion"),
        "vi_name": get_text(root, "./VI/Name"),
        "target": get_text(root, "./Project/TargetClass"),
        "auto_run": get_text(root, "./Project/AutoRunWhenDownloaded"),
        "signature": get_text(root, "SignatureRegister"),
        "expected_md5": expected_md5,
        "actual_md5": actual_md5,
        "md5_ok": md5_ok,
        "bitstream_size": decoded_size,
        "registers": registers,
        "dma_channels": dma_channels,
        "clocks": [clock.get("name", "-") for clock in root.findall(".//UsedBaseClockList/BaseClock")],
        "purpose": infer_purpose(root),
    }


def format_size(size: int | None) -> str:
    if size is None:
        return "-"
    return f"{size:,} 字节 ({size / 1024:.1f} KiB)"


def print_report(info: dict[str, object]) -> None:
    path = info["path"]
    print(f"\n{'=' * 78}\n文件：{path.name}\n路径：{path}")
    print(f"容器大小：{format_size(info['file_size'])}")
    print(f"原始 VI：{info['vi_name']}")
    print(f"FPGA 目标：{info['target']}")
    print(f"格式版本：bitfile {info['bitfile_version']} / bitstream {info['bitstream_version']}")
    print(f"下载后自动运行：{'是' if info['auto_run'] == 'true' else '否'}")
    print(f"接口签名：{info['signature']}")
    print(f"位流大小：{format_size(info['bitstream_size'])}")
    md5_status = "通过" if info["md5_ok"] else "失败或不可用"
    print(f"位流 MD5：{info['expected_md5']}（校验{md5_status}）")
    print("功能判断：")
    for clue in info["purpose"]:
        print(f"  - {clue}")

    user_registers = [register for register in info["registers"] if not register["internal"]]
    print(f"用户寄存器（{len(user_registers)} 个）：")
    for register in user_registers:
        print(
            f"  - {register['name']}: {register['direction']}, {register['type']}, "
            f"{register['bits']} bit, offset={register['offset']}"
        )

    print(f"DMA FIFO（{len(info['dma_channels'])} 个）：")
    if not info["dma_channels"]:
        print("  - 无")
    for channel in info["dma_channels"]:
        print(
            f"  - {channel['name']}: {channel['direction']}, {channel['type']}, "
            f"深度={channel['elements']}"
        )
    print(f"使用时钟：{', '.join(info['clocks']) or '未记录'}")


def main() -> int:
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="解析 NI LabVIEW FPGA .lvbitx 文件")
    parser.add_argument(
        "path",
        nargs="?",
        type=Path,
        default=Path("FPGA Bitfiles"),
        help=".lvbitx 文件或包含它们的目录（默认：FPGA Bitfiles）",
    )
    args = parser.parse_args()

    if args.path.is_file():
        files = [args.path]
    elif args.path.is_dir():
        files = sorted(args.path.rglob("*.lvbitx"))
    else:
        parser.error(f"路径不存在：{args.path}")

    if not files:
        parser.error(f"没有找到 .lvbitx 文件：{args.path}")

    failed = False
    for path in files:
        try:
            print_report(parse_bitfile(path))
        except (ET.ParseError, ValueError, OSError) as exc:
            failed = True
            print(f"解析失败：{path}: {exc}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
