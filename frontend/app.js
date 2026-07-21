document.addEventListener('DOMContentLoaded', () => {
    // Set today's date as default
    const today = new Date().toISOString().split('T')[0];
    document.getElementById('date-input').value = today;

    const generateBtn = document.getElementById('generate-btn');
    const loadingPanel = document.getElementById('loading-panel');
    const resultsPanel = document.getElementById('results-panel');
    const loadingText = document.getElementById('loading-text');

    const updateBadge = (action) => {
        const badge = document.getElementById('signal-action');
        badge.textContent = action.toUpperCase();
        badge.className = 'badge'; // reset
        if (action.toLowerCase().includes('buy')) badge.classList.add('buy');
        else if (action.toLowerCase().includes('sell')) badge.classList.add('sell');
        else badge.classList.add('hold');
    };

    generateBtn.addEventListener('click', async () => {
        const date = document.getElementById('date-input').value;

        // UI State
        resultsPanel.classList.add('hidden');
        loadingPanel.classList.remove('hidden');
        generateBtn.disabled = true;

        // Fun loading text loop
        const phrases = [
            "Gathering market data...",
            "Fundamental Analysts are reviewing...",
            "Technical Analysts are drawing charts...",
            "Bull and Bear agents are debating...",
            "Risk Manager is validating safety...",
            "Portfolio Manager is making final decision..."
        ];
        let phraseIdx = 0;
        const phraseInterval = setInterval(() => {
            phraseIdx = (phraseIdx + 1) % phrases.length;
            loadingText.textContent = phrases[phraseIdx];
        }, 5000);

        try {
            const response = await fetch('http://127.0.0.1:8000/api/scan-and-signal', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({ date })
            });

            if (!response.ok) {
                const err = await response.json();
                throw new Error(err.detail || 'Failed to fetch signal');
            }

            const data = await response.json();
            
            document.getElementById('signal-pair').textContent = data.ticker;
            document.getElementById('signal-reasoning').innerHTML = data.reasoning.replace(/\n/g, '<br>');
            
            document.getElementById('signal-entry').textContent = data.entry || (data.action === "HOLD" ? "WAIT" : "Market Price");
            document.getElementById('signal-tp').textContent = data.tp || (data.action === "HOLD" ? "WAIT" : "--");
            document.getElementById('signal-sl').textContent = data.sl || (data.action === "HOLD" ? "WAIT" : "--");

            if (data.action === "HOLD") {
                document.getElementById('signal-reasoning').innerHTML = "<b>🚨 NO TRADE TODAY:</b> " + data.reasoning.replace(/\n/g, '<br>');
            } else {
                document.getElementById('signal-reasoning').innerHTML = data.reasoning.replace(/\n/g, '<br>');
            }

            updateBadge(data.action);

            clearInterval(phraseInterval);
            loadingPanel.classList.add('hidden');
            resultsPanel.classList.remove('hidden');

        } catch (error) {
            clearInterval(phraseInterval);
            loadingPanel.classList.add('hidden');
            alert(`Error: ${error.message}\nMake sure your backend is running and GOOGLE_API_KEY is set in backend/.env.`);
        } finally {
            generateBtn.disabled = false;
        }
    });
});
