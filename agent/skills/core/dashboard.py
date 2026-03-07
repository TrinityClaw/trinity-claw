import psutil
from datetime import datetime, timedelta

NAME = "dashboard"
DOC = (
    "System health and monitoring: "
    "health()→full snapshot of CPU/memory/disk/network/uptime/top processes; "
    "alerts()→only active threshold warnings, empty means all clear; "
    "processes(n)→top N processes by CPU usage (default 5)."
)


def _build_bar(percent: float, width: int = 10) -> str:
    filled = int(percent / 100 * width)
    empty = width - filled
    icon = "🔴" if percent > 80 else ("🟡" if percent > 50 else "🟢")
    return f"{icon} [{'█' * filled}{'░' * empty}]"


def _uptime() -> str:
    delta = timedelta(seconds=datetime.now().timestamp() - psutil.boot_time())
    days = delta.days
    hours, rem = divmod(delta.seconds, 3600)
    minutes = rem // 60
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)


def health() -> str:
    """Full system snapshot: CPU, memory, disk, network, uptime, top processes."""
    try:
        cpu = psutil.cpu_percent(interval=0.5)
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage('/')
        net = psutil.net_io_counters()

        top_procs = sorted(
            psutil.process_iter(['pid', 'name', 'cpu_percent', 'memory_percent']),
            key=lambda p: p.info.get('cpu_percent') or 0,
            reverse=True,
        )[:3]

        lines = [
            "┌─ SYSTEM HEALTH ────────────────────────────────────┐",
            f"│ CPU       {_build_bar(cpu)} {cpu:5.1f}%",
            f"│ Memory    {_build_bar(mem.percent)} {mem.percent:5.1f}%"
            f"  ({mem.used / 1024**3:.1f}GB / {mem.total / 1024**3:.1f}GB)",
            f"│ Disk      {_build_bar(disk.percent)} {disk.percent:5.1f}%"
            f"  ({disk.used / 1024**3:.1f}GB / {disk.total / 1024**3:.1f}GB)",
            f"│ Network   ↑ {net.bytes_sent / 1024**2:.1f}MB  ↓ {net.bytes_recv / 1024**2:.1f}MB",
            f"│ Uptime    {_uptime()}",
            "│",
            "│ Top Processes:",
        ]

        for p in top_procs:
            try:
                name = (p.info['name'] or 'unknown')[:20]
                cpu_p = p.info.get('cpu_percent') or 0.0
                mem_p = p.info.get('memory_percent') or 0.0
                lines.append(f"│   {name:<20} CPU {cpu_p:5.1f}%  MEM {mem_p:4.1f}%")
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        lines += [
            "│",
            f"│ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "└────────────────────────────────────────────────────┘",
        ]
        return "\n".join(lines)
    except Exception as e:
        return f"❌ Health check failed: {e}"


def alerts() -> str:
    """Return active threshold warnings. '✅ All systems nominal' means no issues."""
    THRESHOLDS = {"cpu": 80.0, "memory": 85.0, "disk": 90.0}
    try:
        cpu = psutil.cpu_percent(interval=0.5)
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage('/')

        warnings = []
        if cpu > THRESHOLDS["cpu"]:
            warnings.append(f"🔴 CPU critical: {cpu:.1f}% (threshold {THRESHOLDS['cpu']}%)")
        if mem.percent > THRESHOLDS["memory"]:
            warnings.append(f"🔴 Memory critical: {mem.percent:.1f}% (threshold {THRESHOLDS['memory']}%)")
        if disk.percent > THRESHOLDS["disk"]:
            warnings.append(f"🔴 Disk critical: {disk.percent:.1f}% (threshold {THRESHOLDS['disk']}%)")

        if not warnings:
            return "✅ All systems nominal"
        return "⚠️ ALERTS:\n" + "\n".join(warnings)
    except Exception as e:
        return f"❌ Alert check failed: {e}"


def processes(n: str = "5") -> str:
    """Top N processes by CPU usage (default 5)."""
    try:
        count = int(n)
    except ValueError:
        count = 5

    try:
        top_procs = sorted(
            psutil.process_iter(['pid', 'name', 'cpu_percent', 'memory_percent']),
            key=lambda p: p.info.get('cpu_percent') or 0,
            reverse=True,
        )[:count]

        lines = [
            f"┌─ TOP {count} PROCESSES ──────────────────────────────────┐",
            f"│ {'PID':<7} {'NAME':<22} {'CPU%':>6}  {'MEM%':>5}",
            "│ " + "─" * 46,
        ]

        for p in top_procs:
            try:
                pid = p.info['pid']
                name = (p.info['name'] or 'unknown')[:22]
                cpu_p = p.info.get('cpu_percent') or 0.0
                mem_p = p.info.get('memory_percent') or 0.0
                lines.append(f"│ {pid:<7} {name:<22} {cpu_p:>6.1f}  {mem_p:>5.1f}%")
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        lines.append("└────────────────────────────────────────────────────┘")
        return "\n".join(lines)
    except Exception as e:
        return f"❌ Process list failed: {e}"


__all__ = ["NAME", "DOC", "health", "alerts", "processes"]
