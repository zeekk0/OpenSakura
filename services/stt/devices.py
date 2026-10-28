"""Pick the compute device for the STT service (CUDA / ROCm / Intel XPU / Apple MPS / CPU)."""

from __future__ import annotations

from typing import Optional


def _safe(fn) -> bool:
    try:
        return bool(fn())
    except Exception:  # noqa: BLE001
        return False


def available(torch) -> dict:
    return {
        "cuda": _safe(lambda: torch.cuda.is_available()),
        "xpu": _safe(lambda: torch.xpu.is_available()),
        "mps": _safe(lambda: torch.backends.mps.is_available()),
    }


def _default_device(av: dict) -> str:
    if av["cuda"]:
        return "cuda:0"
    if av["xpu"]:
        return "xpu:0"
    if av["mps"]:
        return "mps"
    return "cpu"


def resolve_device(requested: Optional[str], torch) -> tuple[str, Optional[str]]:
    req = (requested or "auto").strip().lower()
    av = available(torch)
    if req in ("", "auto"):
        return _default_device(av), None
    if req == "cpu":
        return "cpu", None
    kind = req.split(":", 1)[0]
    if kind in av:
        if av[kind]:
            return req, None
        fallback = _default_device(av)
        return fallback, f"requested device {req!r} is not available in this PyTorch build; using {fallback!r}"
    return "cpu", f"unknown device {req!r} (use auto, cpu, cuda[:N], xpu[:N] or mps); using 'cpu'"


def describe(torch, device: str) -> dict:
    """Facts for /health: what actually runs the model."""
    kind = device.split(":", 1)[0]
    info = {"device": device, "backend": kind, "torch": getattr(torch, "__version__", None)}
    if kind == "cuda":
        info["backend"] = "rocm" if getattr(getattr(torch, "version", None), "hip", None) else "cuda"
        info["gpu_name"] = _name(lambda: torch.cuda.get_device_name(0))
    elif kind == "xpu":
        info["gpu_name"] = _name(lambda: torch.xpu.get_device_name(0))
    elif kind == "mps":
        info["gpu_name"] = "Apple GPU (MPS)"
    return info


def _name(fn) -> Optional[str]:
    try:
        return str(fn())
    except Exception:  # noqa: BLE001
        return None
