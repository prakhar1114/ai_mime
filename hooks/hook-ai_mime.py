"""PyInstaller hook for ai_mime — native libs + full litellm/instructor bundles."""

from PyInstaller.utils.hooks import collect_dynamic_libs, collect_all

binaries = (
    collect_dynamic_libs("sounddevice")
    + collect_dynamic_libs("mss")
    + collect_dynamic_libs("pynput")
    + collect_dynamic_libs("PIL")
)

# litellm & instructor use importlib.resources and lazy __init__ re-exports;
# collect_all is the only reliable way to pull in every submodule + data file.
_litellm_datas, _litellm_bins, _litellm_hidden = collect_all("litellm")
_instr_datas, _instr_bins, _instr_hidden = collect_all("instructor")

datas = _litellm_datas + _instr_datas
binaries += _litellm_bins + _instr_bins
hiddenimports = _litellm_hidden + _instr_hidden
