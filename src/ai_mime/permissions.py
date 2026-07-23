import sys
import mss
import mss.exception
from ai_mime.platform import IS_MAC, IS_WINDOWS

if IS_MAC:
    try:
        import ApplicationServices
        from AppKit import NSAlert, NSInformationalAlertStyle
    except ImportError:
        ApplicationServices = None  # type: ignore[assignment]
        NSAlert = None  # type: ignore[assignment]
else:
    ApplicationServices = None  # type: ignore[assignment]
    NSAlert = None  # type: ignore[assignment]


def show_alert(title: str, message: str) -> None:
    """Show a native alert dialog on macOS or Windows."""
    if IS_MAC and NSAlert is not None:
        try:
            alert = NSAlert.alloc().init()
            alert.setMessageText_(title)
            alert.setInformativeText_(message)
            alert.setAlertStyle_(NSInformationalAlertStyle)
            alert.runModal()
            return
        except Exception:
            pass

    if IS_WINDOWS:
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, message, title, 0x40)
            return
        except Exception:
            pass

    print(f"[{title}] {message}")


def check_accessibility() -> bool:
    """Check if the app has Accessibility permissions (macOS only)."""
    if not IS_MAC:
        return True

    if ApplicationServices is None:
        return True

    try:
        options = {ApplicationServices.kAXTrustedCheckOptionPrompt: True}
        is_trusted = bool(ApplicationServices.AXIsProcessTrustedWithOptions(options))
        if not is_trusted:
            show_alert(
                "Accessibility Permission Required",
                "Please enable Accessibility for this app in System Settings > Privacy & Security > Accessibility.\n\nThen restart the app."
            )
            return False
    except Exception as e:
        print(f"Accessibility check failed: {e}")

    return True


def check_screen_recording() -> bool:
    """Check if the app has Screen Recording permissions."""
    try:
        with mss.mss() as sct:
            monitor = {"top": 0, "left": 0, "width": 1, "height": 1}
            sct.grab(monitor)
            return True
    except (mss.exception.ScreenShotError, OSError):
        show_alert(
            "Screen Recording Permission Required",
            "Please enable Screen Recording for this app in System Settings / System Permissions.\n\nThen restart the app."
        )
        return False
    except Exception as e:
        print(f"Screen recording check failed: {e}")
        show_alert(
            "Screen Recording Error",
            f"Could not verify screen recording permissions: {e}"
        )
        return False


def check_permissions() -> bool:
    """Verify all required permissions."""
    if not check_accessibility():
        return False
    if not check_screen_recording():
        return False
    return True
