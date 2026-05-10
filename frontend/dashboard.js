async function fetchStatus() {
    console.log("Fetching status...");
    try {
        // Fetch from the root-relative path which Nginx maps to the shared folder
        const res = await fetch('/state.json');
        
        if (!res.ok) {
            throw new Error(`HTTP error! status: ${res.status}`);
        }

        const data = await res.json();
        console.log("Received data:", data);

        const cash = data.cash;

        const cashEl = document.getElementById('cash');
        if (cashEl && cash !== undefined && cash !== null) {
            // Handle cases where cash might be the sys.float_info.max fallback
            if (cash > 1e15) {
                cashEl.textContent = "₪-- (Not Loaded)";
                cashEl.className = "value";
            } else {
                cashEl.textContent = `₪${Number(cash).toLocaleString()}`;
                cashEl.className = `value ${cash >= 0 ? 'cash-positive' : 'cash-negative'}`;
            }
        }

        const lastUpdatedEl = document.getElementById('last-updated');
        if (lastUpdatedEl) {
            if (data.last_updated) {
                lastUpdatedEl.textContent = `Last updated: ${data.last_updated}`;
            } else {
                lastUpdatedEl.textContent = `Last updated: ${new Date().toLocaleTimeString()}`;
            }
        }
    } catch (e) {
        console.error("Dashboard fetch error:", e);
        const lastUpdatedEl = document.getElementById('last-updated');
        if (lastUpdatedEl) {
            lastUpdatedEl.textContent = `Waiting for backend... (${e.message})`;
        }
    }
}

// Initial fetch
fetchStatus();
// Update every 2 seconds
setInterval(fetchStatus, 2000);
