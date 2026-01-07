import ApplicationServices
import mss
import mss.exception
from AppKit import NSAlert, NSInformationalAlertStyle

def show_alert(title, message):
    """Show a native macOS alert dialog."""
    alert = NSAlert.alloc().init()
    alert.setMessageText_(title)
    alert.setInformativeText_(message)
    alert.setAlertStyle_(NSInformationalAlertStyle)
    alert.runModal()

def check_accessibility():
    """Check if the app has Accessibility permissions."""
    # kAXTrustedCheckOptionPrompt=True will trigger the system prompt if not trusted
    options = {ApplicationServices.kAXTrustedCheckOptionPrompt: True}
    is_trusted = ApplicationServices.AXIsProcessTrustedWithOptions(options)

    if not is_trusted:
        show_alert(
            "Accessibility Permission Required",
            "Please enable Accessibility for this app in System Settings > Privacy & Security > Accessibility.\n\nThen restart the app."
        )
        return False
    return True

def check_screen_recording():
    """Check if the app has Screen Recording permissions."""
    try:
        with mss.mss() as sct:
            # Try capturing a 1x1 pixel from the top left to test access
            monitor = {"top": 0, "left": 0, "width": 1, "height": 1}
            sct.grab(monitor)
            return True
    except (mss.exception.ScreenShotError, OSError):
        # On macOS, lack of permission might raise ScreenShotError or just produce black image
        # (though mss usually errors out if it can't access display services).
        # We'll assume error means no permission or display issue.
        show_alert(
            "Screen Recording Permission Required",
            "Please enable Screen Recording for this app in System Settings > Privacy & Security > Screen Recording.\n\nThen restart the app."
        )
        return False
    except Exception as e:
        # Catch-all
        print(f"Screen recording check failed: {e}")
        show_alert(
            "Screen Recording Error",
            f"Could not verify screen recording permissions: {e}"
        )
        return False

def check_permissions():
    """Verify all required permissions."""
    if not check_accessibility():
        return False
    if not check_screen_recording():
        return False
    return True
