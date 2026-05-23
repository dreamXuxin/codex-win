import csv
import io
import queue
import threading
import tkinter as tk
from dataclasses import dataclass
from datetime import datetime
from tkinter import ttk
from urllib.parse import quote
from urllib.request import Request, urlopen


REFRESH_SECONDS = 60
STOOQ_URL = "https://stooq.com/q/l/?s={symbol}&f=sd2t2ocp&h&e=csv"


@dataclass(frozen=True)
class Ticker:
    name: str
    symbol: str
    suffix: str = ""


TICKERS = (
    Ticker("Nasdaq Composite", "^NDQ"),
    Ticker("S&P 500", "^SPX"),
    Ticker("Gold", "XAUUSD", " USD/oz"),
)


class MarketWidget(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Market Watch")
        self.geometry("330x250+60+80")
        self.minsize(300, 230)
        self.configure(bg="#111318")
        self.attributes("-topmost", True)

        self._drag_start_x = 0
        self._drag_start_y = 0
        self._rows = {}
        self._status = tk.StringVar(value="Loading...")
        self._refreshing = False
        self._result_queue = queue.Queue()
        self._next_refresh_at = 0.0

        self._build_ui()
        self._bind_drag()
        self.after(250, self._poll_results)
        self.after(1000, self._tick_status)
        self.refresh()

    def _build_ui(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TFrame", background="#111318")
        style.configure("Title.TLabel", background="#111318", foreground="#f4f7fb", font=("Segoe UI", 13, "bold"))
        style.configure("Muted.TLabel", background="#111318", foreground="#8f98a8", font=("Segoe UI", 9))
        style.configure("Name.TLabel", background="#171a21", foreground="#d8dee9", font=("Segoe UI", 10, "bold"))
        style.configure("Value.TLabel", background="#171a21", foreground="#f4f7fb", font=("Segoe UI", 15, "bold"))
        style.configure("Up.TLabel", background="#171a21", foreground="#35c46f", font=("Segoe UI", 10, "bold"))
        style.configure("Down.TLabel", background="#171a21", foreground="#ff5f57", font=("Segoe UI", 10, "bold"))
        style.configure("Flat.TLabel", background="#171a21", foreground="#aeb6c4", font=("Segoe UI", 10, "bold"))

        header = ttk.Frame(self, padding=(14, 12, 14, 4))
        header.pack(fill="x")

        title = ttk.Label(header, text="Market Watch", style="Title.TLabel")
        title.pack(side="left")

        close_button = tk.Button(
            header,
            text="X",
            command=self.destroy,
            bg="#111318",
            fg="#8f98a8",
            activebackground="#242936",
            activeforeground="#f4f7fb",
            bd=0,
            padx=8,
            pady=2,
            font=("Segoe UI", 10, "bold"),
            cursor="hand2",
        )
        close_button.pack(side="right")

        body = ttk.Frame(self, padding=(12, 4, 12, 8))
        body.pack(fill="both", expand=True)

        for ticker in TICKERS:
            row = tk.Frame(body, bg="#171a21", highlightthickness=1, highlightbackground="#242936")
            row.pack(fill="x", pady=5)
            row.grid_columnconfigure(1, weight=1)

            name = ttk.Label(row, text=ticker.name, style="Name.TLabel")
            name.grid(row=0, column=0, sticky="w", padx=10, pady=(8, 0))

            value = ttk.Label(row, text="--", style="Value.TLabel", anchor="e")
            value.grid(row=0, column=1, sticky="e", padx=10, pady=(8, 0))

            meta = ttk.Label(row, text=ticker.symbol, style="Muted.TLabel")
            meta.grid(row=1, column=0, sticky="w", padx=10, pady=(0, 8))

            change = ttk.Label(row, text="--", style="Flat.TLabel", anchor="e")
            change.grid(row=1, column=1, sticky="e", padx=10, pady=(0, 8))

            self._rows[ticker.symbol] = {
                "value": value,
                "change": change,
                "meta": meta,
                "suffix": ticker.suffix,
            }

        footer = ttk.Frame(self, padding=(14, 0, 14, 10))
        footer.pack(fill="x")

        status = ttk.Label(footer, textvariable=self._status, style="Muted.TLabel")
        status.pack(side="left")

        refresh_button = tk.Button(
            footer,
            text="Refresh",
            command=self.refresh,
            bg="#242936",
            fg="#d8dee9",
            activebackground="#303747",
            activeforeground="#ffffff",
            bd=0,
            padx=9,
            pady=4,
            font=("Segoe UI", 9),
            cursor="hand2",
        )
        refresh_button.pack(side="right")

    def _bind_drag(self) -> None:
        self.bind("<ButtonPress-1>", self._start_drag)
        self.bind("<B1-Motion>", self._drag)

    def _start_drag(self, event: tk.Event) -> None:
        self._drag_start_x = event.x
        self._drag_start_y = event.y

    def _drag(self, event: tk.Event) -> None:
        x = self.winfo_x() + event.x - self._drag_start_x
        y = self.winfo_y() + event.y - self._drag_start_y
        self.geometry(f"+{x}+{y}")

    def refresh(self) -> None:
        if self._refreshing:
            return
        self._refreshing = True
        self._status.set("Refreshing...")
        thread = threading.Thread(target=self._load_quotes, daemon=True)
        thread.start()

    def _load_quotes(self) -> None:
        results = {}
        error = None
        for ticker in TICKERS:
            try:
                results[ticker.symbol] = fetch_quote(ticker.symbol)
            except Exception as exc:  # noqa: BLE001 - surfaced in the UI.
                error = str(exc)
        self._result_queue.put((results, error))

    def _poll_results(self) -> None:
        try:
            while True:
                results, error = self._result_queue.get_nowait()
                self._apply_results(results, error)
        except queue.Empty:
            pass
        self.after(250, self._poll_results)

    def _apply_results(self, results: dict, error: str | None) -> None:
        for symbol, quote_data in results.items():
            row = self._rows[symbol]
            close = quote_data["close"]
            previous = quote_data["previous"]
            change = close - previous
            pct = (change / previous * 100) if previous else 0.0
            style = "Up.TLabel" if change > 0 else "Down.TLabel" if change < 0 else "Flat.TLabel"
            sign = "+" if change > 0 else ""

            row["value"].configure(text=f"{close:,.2f}{row['suffix']}")
            row["change"].configure(text=f"{sign}{change:,.2f}  {sign}{pct:.2f}%", style=style)
            row["meta"].configure(text=f"{symbol}  {quote_data['date']} {quote_data['time']}")

        self._refreshing = False
        self._next_refresh_at = datetime.now().timestamp() + REFRESH_SECONDS
        if error and not results:
            self._status.set("Update failed")
        elif error:
            self._status.set("Partially updated")
        else:
            now = datetime.now().strftime("%H:%M:%S")
            self._status.set(f"Updated {now} | Next {REFRESH_SECONDS}s")
        self.after(REFRESH_SECONDS * 1000, self.refresh)

    def _tick_status(self) -> None:
        if not self._refreshing and self._next_refresh_at:
            remaining = max(0, int(self._next_refresh_at - datetime.now().timestamp()))
            current = self._status.get().split(" | ")[0]
            self._status.set(f"{current} | Next {remaining}s")
        self.after(1000, self._tick_status)


def fetch_quote(symbol: str) -> dict:
    url = STOOQ_URL.format(symbol=quote(symbol, safe=""))
    request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(request, timeout=12) as response:
        payload = response.read().decode("utf-8-sig")

    rows = list(csv.DictReader(io.StringIO(payload)))
    if not rows:
        raise RuntimeError(f"No quote returned for {symbol}")

    row = rows[0]
    if row.get("Close") in (None, "N/D") or row.get("Prev") in (None, "N/D"):
        raise RuntimeError(f"Quote unavailable for {symbol}")

    return {
        "date": row.get("Date", ""),
        "time": row.get("Time", ""),
        "open": float(row["Open"]),
        "close": float(row["Close"]),
        "previous": float(row["Prev"]),
    }


if __name__ == "__main__":
    app = MarketWidget()
    app.mainloop()
