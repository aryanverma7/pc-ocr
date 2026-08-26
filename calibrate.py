"""
Calibration tool (run once, or whenever the capture region needs
updating) - Task #8's gaming-PC side.

Deliberately NOT a live overlay on top of the running game. Valorant
typically runs in a fullscreen/exclusive-fullscreen mode, which can
prevent other windows from reliably drawing on top of it at all. Instead:
take a screenshot of whatever's currently on screen FIRST, then show that
captured image in a normal window for you to drag a selection box over.

Usage:
  1. Get into a Valorant match, and get to the buy phase so the credit
     number is actually visible on screen.
  2. Run this script. It gives you a few seconds to alt-tab back to the
     game (or just leave it visible, if Valorant is in borderless-windowed
     mode) before it takes the screenshot.
  3. A window opens showing exactly what was captured. Click and drag a
     box around the credit number.
  4. Close the window (or press Enter) to save.
"""
import time
import tkinter as tk
from PIL import ImageGrab, ImageTk

from region import normalize_region, save_region

COUNTDOWN_SECONDS = 3


def countdown():
    for remaining in range(COUNTDOWN_SECONDS, 0, -1):
        print(f"Capturing in {remaining}... (switch to Valorant's buy screen now)")
        time.sleep(1)
    print("Capturing now.")


def run_calibration():
    countdown()
    screenshot = ImageGrab.grab()

    root = tk.Tk()
    root.title("Drag a box around the credit number, then close this window")

    photo = ImageTk.PhotoImage(screenshot)
    canvas = tk.Canvas(root, width=screenshot.width, height=screenshot.height)
    canvas.pack()
    canvas.create_image(0, 0, anchor="nw", image=photo)

    selection = {"start": None, "rect_id": None, "result": None}

    def on_press(event):
        selection["start"] = (event.x, event.y)
        if selection["rect_id"] is not None:
            canvas.delete(selection["rect_id"])

    def on_drag(event):
        if selection["rect_id"] is not None:
            canvas.delete(selection["rect_id"])
        x1, y1 = selection["start"]
        selection["rect_id"] = canvas.create_rectangle(
            x1, y1, event.x, event.y, outline="#34f5c5", width=3
        )

    def on_release(event):
        x1, y1 = selection["start"]
        try:
            selection["result"] = normalize_region(x1, y1, event.x, event.y)
            print(f"Selected region: {selection['result']}")
        except ValueError as e:
            print(f"Invalid selection: {e} - try dragging a real rectangle again.")

    canvas.bind("<ButtonPress-1>", on_press)
    canvas.bind("<B1-Motion>", on_drag)
    canvas.bind("<ButtonRelease-1>", on_release)

    root.mainloop()

    if selection["result"] is None:
        print("No region was selected - nothing saved. Run this again to retry.")
        return

    save_region(selection["result"])
    print(f"Saved calibrated region: {selection['result']}")
    print("You can now run agent.py.")


if __name__ == "__main__":
    run_calibration()
