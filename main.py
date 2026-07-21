import csv
import sys
import time
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib.ticker import MultipleLocator
import serial
import serial.tools.list_ports

BAUD_RATE = 9600
SAMPLE_INTERVAL = 0.05
STABLE_DURATION = 4.5
TRIM_TAIL_SECONDS = 4.0
WEIGHT_THRESHOLD = Decimal("0.1")
RESULTS_DIR = Path("results")


def ensure_stdin() -> None:
    if sys.stdin is None:
        raise RuntimeError(
            "标准输入不可用。本程序需要在控制台中运行，"
            "请使用 --console 模式打包（不要使用 --windowed），"
            "并在 cmd 或 PowerShell 中启动。"
        )


def prompt(message: str) -> str:
    ensure_stdin()
    return input(message)


def list_serial_ports() -> list[str]:
    ports = [port.device for port in serial.tools.list_ports.comports()]
    return sorted(ports)


def parse_weight(line: bytes) -> Decimal:
    text = line.decode("ascii", errors="replace").strip()
    if not text:
        raise ValueError("empty weight frame")
    return Decimal(text)


def read_weight(ser: serial.Serial) -> Decimal:
    line = ser.read_until(b"\n")
    if not line:
        raise serial.SerialException("no data received from scale")
    return parse_weight(line)


def read_latest_weight(ser: serial.Serial) -> Decimal:
    latest: Decimal | None = None

    while ser.in_waiting:
        line = ser.readline()
        if not line:
            break
        try:
            latest = parse_weight(line)
        except (ValueError, InvalidOperation):
            continue

    if latest is not None:
        return latest

    return read_weight(ser)


def connect_serial(port: str) -> serial.Serial | None:
    try:
        ser = serial.Serial(
            port=port,
            baudrate=BAUD_RATE,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=2,
        )
        return ser
    except serial.SerialException as exc:
        print(f"无法连接端口 {port}: {exc}")
        return None


def reconnect_serial(ser: serial.Serial) -> serial.Serial:
    # port = ser.port
    if ser.is_open:
        ser.reset_input_buffer()
        ser.reset_output_buffer()
    return ser

    # new_ser = connect_serial(port)
    # if new_ser is None:
    #     raise serial.SerialException(f"无法重新连接端口 {port}")

    # print(f"已重新连接 {port}，准备采集。")
    # return new_ser


def select_port() -> serial.Serial:
    while True:
        ports = list_serial_ports()
        if not ports:
            print("未检测到可用串口，请检查 USB-RS232 连接后重试。")
            prompt("按回车键重新扫描...")
            continue

        print("\n可用串口：")
        for index, port in enumerate(ports, start=1):
            print(f"  {index}. {port}")

        choice = prompt("请输入数字选择端口: ").strip()
        if not choice.isdigit():
            print("请输入有效数字。")
            continue

        index = int(choice)
        if index < 1 or index > len(ports):
            print("选择超出范围，请重新输入。")
            continue

        ser = connect_serial(ports[index - 1])
        if ser is not None:
            print(f"已成功连接 {ports[index - 1]}")
            return ser


def wait_for_weight_start(ser: serial.Serial) -> tuple[datetime, Decimal]:
    print("等待电子秤称重（重量 > 0.1）...")
    while True:
        try:
            weight = read_weight(ser)
        except (serial.SerialException, ValueError, InvalidOperation) as exc:
            print(f"读取重量失败: {exc}")
            continue

        if weight > WEIGHT_THRESHOLD:
            start_time = datetime.now()
            print(f"检测到重量: {weight}，开始采集。")
            return start_time, weight


def collect_samples(ser: serial.Serial) -> list[tuple[datetime, Decimal]]:
    _, first_weight = wait_for_weight_start(ser)
    samples: list[tuple[datetime, Decimal]] = [(datetime.now(), first_weight)]

    last_weight = first_weight
    last_change_time = time.monotonic()
    weight_changed = True

    while True:
        time.sleep(SAMPLE_INTERVAL)

        try:
            weight = read_latest_weight(ser)
        except (serial.SerialException, ValueError, InvalidOperation) as exc:
            print(f"读取重量失败，跳过本次采样: {exc}")
            continue

        now = datetime.now()
        samples.append((now, weight))

        if weight != last_weight:
            last_weight = weight
            last_change_time = time.monotonic()
            weight_changed = True
            print(f"重量变化: {weight}")
        elif weight_changed and time.monotonic() - last_change_time >= STABLE_DURATION:
            print(f"重量已连续 {STABLE_DURATION:.0f} 秒未变化，结束采集。")
            break

    return samples


def trim_samples(samples: list[tuple[datetime, Decimal]]) -> list[tuple[datetime, Decimal]]:
    end_time = samples[-1][0]
    cutoff_time = end_time - timedelta(seconds=TRIM_TAIL_SECONDS)
    trimmed = [(ts, w) for ts, w in samples if ts <= cutoff_time]
    return trimmed if trimmed else samples


def _interpolate_weight(samples: list[tuple[datetime, Decimal]], target_time: datetime) -> float:
    if target_time <= samples[0][0]:
        return float(samples[0][1])
    if target_time >= samples[-1][0]:
        return float(samples[-1][1])

    for i in range(len(samples) - 1):
        t1, w1 = samples[i]
        t2, w2 = samples[i + 1]
        if t1 <= target_time <= t2:
            ratio = (target_time - t1).total_seconds() / (t2 - t1).total_seconds()
            return float(w1) + ratio * (float(w2) - float(w1))

    return float(samples[-1][1])


def compute_weight_rate(
    samples: list[tuple[datetime, Decimal]], window_seconds: float = 0.5
) -> tuple[list[float], list[float]]:
    if len(samples) < 2:
        return [], []

    start_time = samples[0][0]
    total_seconds = (samples[-1][0] - start_time).total_seconds()

    times_s: list[float] = []
    rates: list[float] = []

    t = window_seconds
    while t <= total_seconds:
        t_prev = start_time + timedelta(seconds=t - window_seconds)
        t_now = start_time + timedelta(seconds=t)

        w_prev = _interpolate_weight(samples, t_prev)
        w_now = _interpolate_weight(samples, t_now)
        rate = (w_now - w_prev) / window_seconds

        times_s.append(t)
        rates.append(rate)
        t += window_seconds

    return times_s, rates


def _draw_chart(
    times: list[float],
    values: list[float],
    ylabel: str,
    title: str,
    output_path: Path,
    xlim: tuple[float, float] | None = None,
    ylim: tuple[float, float] | None = None,
    x_major_step: float = 1,
    y_major_step: float | None = None,
) -> None:
    plt.figure(figsize=(10, 5))
    ax = plt.gca()
    ax.plot(times, values, marker="o", markersize=3, linewidth=1)
    ax.set_xlabel("Time (Seconds)")
    ax.set_ylabel(ylabel)
    ax.set_title(title)
    ax.xaxis.set_major_locator(MultipleLocator(x_major_step))
    if y_major_step is not None:
        ax.yaxis.set_major_locator(MultipleLocator(y_major_step))
    if xlim is not None:
        ax.set_xlim(xlim)
    elif times:
        ax.set_xlim(0, int(max(times)) + 1)
    if ylim is not None:
        ax.set_ylim(ylim)
    ax.grid(True, alpha=0.3)
    plt.tight_layout()
    plt.savefig(output_path, dpi=150)
    plt.close()


def save_chart(samples: list[tuple[datetime, Decimal]]) -> Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    trimmed = trim_samples(samples)
    start_time = trimmed[0][0]
    times = [(ts - start_time).total_seconds() for ts, _ in trimmed]
    weights = [float(w) for _, w in trimmed]

    filename = datetime.now().strftime("%Y%m%d%H%M") + ".png"
    output_path = RESULTS_DIR / filename
    _draw_chart(times, weights, "Weight (Grams)", "Flow Curve", output_path)
    return output_path


def save_rate_chart(samples: list[tuple[datetime, Decimal]], base_path: Path) -> Path | None:
    trimmed = trim_samples(samples)
    times_s, rates = compute_weight_rate(trimmed, window_seconds=0.5)
    if not times_s:
        return None

    rate_path = base_path.with_name(base_path.stem + "_rate" + base_path.suffix)
    _draw_chart(
        times_s,
        rates,
        "Weight Change Rate (g/s)",
        "Flow Rate (0.5s window)",
        rate_path,
        xlim=(0, 30),
        ylim=(0, 30),
        x_major_step=1,
        y_major_step=1,
    )
    return rate_path


def save_csv(samples: list[tuple[datetime, Decimal]], base_path: Path) -> Path:
    trimmed = trim_samples(samples)
    start_time = trimmed[0][0]
    times_s = [(ts - start_time).total_seconds() for ts, _ in trimmed]
    weights = [float(w) for _, w in trimmed]

    rate_times_s, rates = compute_weight_rate(trimmed, window_seconds=0.5)

    csv_path = base_path.with_suffix(".csv")
    with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["time_s", "weight_g", "rate_time_s", "rate_g_per_s"])
        max_len = max(len(times_s), len(rate_times_s))
        for i in range(max_len):
            row = [
                times_s[i] if i < len(times_s) else "",
                weights[i] if i < len(weights) else "",
                rate_times_s[i] if i < len(rate_times_s) else "",
                rates[i] if i < len(rates) else "",
            ]
            writer.writerow(row)

    return csv_path


def run_collection(ser: serial.Serial) -> serial.Serial:
    ser = reconnect_serial(ser)
    samples = collect_samples(ser)
    output_path = save_chart(samples)
    rate_path = save_rate_chart(samples, output_path)
    csv_path = save_csv(samples, output_path)
    print(f"采集完成，共 {len(samples)} 条数据")
    print(f"  重量曲线: {output_path}")
    if rate_path:
        print(f"  变化率曲线: {rate_path}")
    print(f"  原始数据: {csv_path}")
    return ser


def main() -> None:
    ser = select_port()

    try:
        while True:
            command = prompt("\n输入 1 开始采集，输入 q 退出: ").strip()
            if command.lower() == "q":
                break
            if command != "1":
                print("无效输入，请输入 1 或 q。")
                continue

            ser = run_collection(ser)
    finally:
        ser.close()
        print("串口已关闭。")


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as exc:
        print(exc)
        if sys.stdin is not None:
            input("\n按回车键退出...")
