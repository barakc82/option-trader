async function fetchStatus(): Promise<void> {
    try {
        const res = await fetch('/state.json');
        const data = await res.json();

        const cash = data.cash;

        const cashEl = document.getElementById('cash')!;
        cashEl.textContent = `₪${cash.toLocaleString()}`;
        cashEl.className = `value ${cash >= 0 ? 'cash-positive' : 'cash-negative'}`;

        document.getElementById('last-updated')!.textContent =
            `Last updated: ${data.last_updated}`;
    } catch (e) {
        document.getElementById('last-updated')!.textContent = `Error: ${e}`;
    }
}

fetchStatus();
setInterval(fetchStatus, 2000);