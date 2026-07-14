import time
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

import matplotlib.pyplot as plt
import serial
import serial.tools.list_ports

BAUD_RATE = 9600
SAMPLE_INTERVAL = 0.1
STABLE_DURATION = 5.0
WEIGHT_THRESHOLD = Decimal("0.1")
RESULTS_DIR = Path("results")


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


def select_port() -> serial.Serial:
    while True:
        ports = list_serial_ports()
        if not ports:
            print("未检测到可用串口，请检查 USB-RS232 连接后重试。")
            input("按回车键重新扫描...")
            continue

        print("\n可用串口：")
        for index, port in enumerate(ports, start=1):
            print(f"  {index}. {port}")

        choice = input("请输入数字选择端口: ").strip()
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
            weight = read_weight(ser)
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


def save_chart(samples: list[tuple[datetime, Decimal]]) -> Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    start_time = samples[0][0]
    times = [(ts - start_time).total_seconds() * 1000 for ts, _ in samples]
    weights = [float(w) for _, w in samples]

    plt.figure(figsize=(10, 5))
    plt.plot(times, weights, marker="o", markersize=3, linewidth=1)
    plt.xlabel("Time (Milliseconds)")
    plt.ylabel("Weight (Grams)")
    plt.title("Flow Curve")
    plt.grid(True, alpha=0.3)
    plt.tight_layout()

    filename = datetime.now().strftime("%Y%m%d%H%M") + ".png"
    output_path = RESULTS_DIR / filename
    plt.savefig(output_path, dpi=150)
    plt.close()

    return output_path


def run_collection(ser: serial.Serial) -> None:
    samples = collect_samples(ser)
    output_path = save_chart(samples)
    print(f"采集完成，共 {len(samples)} 条数据，图表已保存至 {output_path}")


def main() -> None:
    ser = select_port()

    try:
        while True:
            command = input('\n输入 1 开始采集，输入 q 退出: ').strip()
            if command.lower() == "q":
                break
            if command != "1":
                print("无效输入，请输入 1 或 q。")
                continue

            run_collection(ser)
    finally:
        ser.close()
        print("串口已关闭。")


if __name__ == "__main__":
    main()
