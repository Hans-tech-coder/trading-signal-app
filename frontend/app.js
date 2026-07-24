document.addEventListener('DOMContentLoaded', () => {
    // Set today's date as default
    const today = new Date().toISOString().split('T')[0];
    document.getElementById('date-input').value = today;

    const generateBtn = document.getElementById('generate-btn');
    const refreshStatsBtn = document.getElementById('refresh-stats-btn');
    const loadingPanel = document.getElementById('loading-panel');
    const resultsPanel = document.getElementById('results-panel');
    const loadingText = document.getElementById('loading-text');

    let winRateChartInstance = null; // To store chart instance and destroy it on reload

    const updateBadge = (action) => {
        const badge = document.getElementById('signal-action');
        badge.textContent = action.toUpperCase();
        badge.className = 'badge'; // reset
        if (action.toLowerCase().includes('buy')) badge.classList.add('buy');
        else if (action.toLowerCase().includes('sell')) badge.classList.add('sell');
        else badge.classList.add('hold');
    };

    const loadStats = async () => {
        try {
            // First, trigger evaluation
            await fetch('http://127.0.0.1:8000/api/evaluate-trades');
            
            // Then fetch stats
            const response = await fetch('http://127.0.0.1:8000/api/trade-stats');
            const data = await response.json();
            
            if(data.status === "success") {
                const stats = data.data;
                document.getElementById('stat-winrate').textContent = stats.win_rate + '%';
                document.getElementById('stat-total').textContent = stats.total;
                document.getElementById('stat-won').textContent = stats.won;
                document.getElementById('stat-lost').textContent = stats.lost;
                
                const list = document.getElementById('recent-trades-list');
                list.innerHTML = '';
                if(stats.recent_trades.length === 0) {
                    list.innerHTML = '<li style="color: var(--text-secondary);">No trades yet.</li>';
                } else {
                    stats.recent_trades.forEach(t => {
                        const li = document.createElement('li');
                        li.style.padding = '0.5rem 0';
                        li.style.borderBottom = '1px solid rgba(255,255,255,0.05)';
                        li.style.display = 'flex';
                        li.style.justifyContent = 'space-between';
                        
                        let statusColor = 'var(--text-secondary)';
                        if (t.status === 'WON') statusColor = 'var(--success)';
                        if (t.status === 'LOST') statusColor = 'var(--danger)';
                        
                        li.innerHTML = `
                            <span>${t.date} <b>${t.ticker}</b> - ${t.action} (Lot: ${t.lot_size || 0}, RRR: 1:${t.rrr || 0})</span>
                            <span style="color: ${statusColor}; font-weight: bold;">${t.status}</span>
                        `;
                        list.appendChild(li);
                    });
                }
            }

            // Also load Advanced Analytics (Chart)
            const analyticsResponse = await fetch('http://127.0.0.1:8000/api/analytics');
            const analyticsData = await analyticsResponse.json();
            
            if(analyticsData.status === "success" && Object.keys(analyticsData.data.asset_performance).length > 0) {
                renderWinRateChart(analyticsData.data.asset_performance);
            }

        } catch (error) {
            console.error("Error loading stats:", error);
        }
    };

    const renderWinRateChart = (performanceData) => {
        const ctx = document.getElementById('winRateChart').getContext('2d');
        
        const labels = Object.keys(performanceData);
        const data = Object.values(performanceData).map(d => d.win_rate);
        
        if (winRateChartInstance) {
            winRateChartInstance.destroy();
        }
        
        winRateChartInstance = new Chart(ctx, {
            type: 'bar',
            data: {
                labels: labels,
                datasets: [{
                    label: 'Win Rate (%)',
                    data: data,
                    backgroundColor: 'rgba(99, 102, 241, 0.5)',
                    borderColor: 'rgba(99, 102, 241, 1)',
                    borderWidth: 1,
                    borderRadius: 4
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                scales: {
                    y: {
                        beginAtZero: true,
                        max: 100,
                        grid: { color: 'rgba(255, 255, 255, 0.05)' },
                        ticks: { color: '#888' }
                    },
                    x: {
                        grid: { display: false },
                        ticks: { color: '#888' }
                    }
                },
                plugins: {
                    legend: { display: false }
                }
            }
        });
    };

    // Load stats on app startup
    loadStats();

    refreshStatsBtn.addEventListener('click', async () => {
        refreshStatsBtn.textContent = 'Refreshing...';
        refreshStatsBtn.disabled = true;
        await loadStats();
        refreshStatsBtn.textContent = 'Refresh & Evaluate';
        refreshStatsBtn.disabled = false;
    });

    generateBtn.addEventListener('click', async () => {
        const date = document.getElementById('date-input').value;
        const balance = parseFloat(document.getElementById('balance-input').value) || 1000;
        const risk = parseFloat(document.getElementById('risk-input').value) || 1.0;

        resultsPanel.classList.add('hidden');
        loadingPanel.classList.remove('hidden');

        // Dynamic Loading Text
        const phrases = [
            "AI is scanning global forex charts...",
            "Calculating 28-Cross Currency Strength Engine...",
            "Checking Currency Strength Matrix...",
            "Checking Economic Calendar & Macro Events...",
            "Calculating VWAP and Bollinger Bands...",
            "Risk Manager is validating safety...",
            "Computing MT5 Lot Size and RRR..."
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
                body: JSON.stringify({ 
                    date: date,
                    account_balance: balance,
                    risk_percentage: risk
                })
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
            document.getElementById('signal-lotsize').textContent = data.lot_size ? data.lot_size : "--";
            document.getElementById('signal-rrr').textContent = data.rrr ? `1:${data.rrr}` : "--";

            if (data.action === "HOLD") {
                document.getElementById('signal-reasoning').innerHTML = "<b>🚨 NO TRADE TODAY:</b> " + data.reasoning.replace(/\n/g, '<br>');
                document.getElementById('execution-box').style.display = 'none';
            } else {
                document.getElementById('signal-reasoning').innerHTML = data.reasoning.replace(/\n/g, '<br>');
                document.getElementById('execution-box').style.display = 'block';
                document.getElementById('execution-status').textContent = '';
                document.getElementById('execute-mt5-btn').disabled = false;
                
                // Store signal data globally for execution
                window.currentSignal = {
                    action: data.action,
                    symbol: data.ticker,
                    entry: data.entry,
                    sl: data.sl,
                    tp: data.tp
                };
            }

            updateBadge(data.action);
            
            // Display Heatmap
            if (data.currency_strength && Object.keys(data.currency_strength).length > 0) {
                const cs = data.currency_strength;
                if (cs["1H"] && cs["1H"].strongest) {
                    document.getElementById('heatmap-1h-strong').textContent = `${cs["1H"].strongest} (+${cs["1H"].strongest_val}%)`;
                    document.getElementById('heatmap-1h-weak').textContent = `${cs["1H"].weakest} (${cs["1H"].weakest_val}%)`;
                }
                if (cs["4H"] && cs["4H"].strongest) {
                    document.getElementById('heatmap-4h-strong').textContent = `${cs["4H"].strongest} (+${cs["4H"].strongest_val}%)`;
                    document.getElementById('heatmap-4h-weak').textContent = `${cs["4H"].weakest} (${cs["4H"].weakest_val}%)`;
                }
                if (cs["24H"] && cs["24H"].strongest) {
                    document.getElementById('heatmap-24h-strong').textContent = `${cs["24H"].strongest} (+${cs["24H"].strongest_val}%)`;
                    document.getElementById('heatmap-24h-weak').textContent = `${cs["24H"].weakest} (${cs["24H"].weakest_val}%)`;
                }
                document.getElementById('heatmap-panel').style.display = 'block';
            }
            
            // Display Macro and News Status
            if (data.news_status || data.macro) {
                const macroPanel = document.getElementById('macro-panel');
                const newsBadge = document.getElementById('news-status-badge');
                
                if (data.news_status) {
                    document.getElementById('news-message').textContent = data.news_status.message;
                    if (data.news_status.has_warning) {
                        newsBadge.textContent = "WARNING";
                        newsBadge.style.background = "var(--danger)";
                    } else {
                        newsBadge.textContent = "CLEAR";
                        newsBadge.style.background = "var(--success)";
                    }
                }
                
                if (data.macro) {
                    document.getElementById('macro-message').textContent = data.macro.replace("Central Bank Rates: ", "");
                }
                
                macroPanel.style.display = 'block';
            }

            clearInterval(phraseInterval);
            loadingPanel.classList.add('hidden');
            resultsPanel.classList.remove('hidden');
            
            // Refresh stats after new trade
            loadStats();

        } catch (error) {
            clearInterval(phraseInterval);
            loadingPanel.classList.add('hidden');
            alert(`Error: ${error.message}\nMake sure your backend is running and GOOGLE_API_KEY is set in backend/.env.`);
        } finally {
            generateBtn.disabled = false;
        }
    });

    // AI Mentor Logic
    const askMentorBtn = document.getElementById('ask-mentor-btn');
    const mentorBox = document.getElementById('mentor-feedback-box');
    const mentorText = document.getElementById('mentor-text');

    askMentorBtn.addEventListener('click', async () => {
        askMentorBtn.disabled = true;
        askMentorBtn.textContent = 'Consulting AI...';
        mentorBox.style.display = 'block';
        mentorText.textContent = 'Coach is analyzing your trade history...';

        try {
            const response = await fetch('http://127.0.0.1:8000/api/ai-mentor');
            const data = await response.json();
            
            if(data.status === "success") {
                mentorText.innerHTML = marked(data.feedback) || data.feedback; 
            } else {
                mentorText.textContent = "Error getting feedback.";
            }
        } catch (error) {
            mentorText.textContent = "Failed to connect to AI Mentor.";
        } finally {
            askMentorBtn.disabled = false;
            askMentorBtn.textContent = 'Ask Mentor AI';
        }
    });

    // MT5 Execution Logic
    const executeBtn = document.getElementById('execute-mt5-btn');
    const executeStatus = document.getElementById('execution-status');
    
    executeBtn.addEventListener('click', async () => {
        if (!window.currentSignal) return;
        
        executeBtn.disabled = true;
        executeStatus.style.color = 'var(--text-secondary)';
        executeStatus.textContent = 'Sending order to MT5...';
        
        try {
            const response = await fetch('http://127.0.0.1:8000/api/execute-trade', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(window.currentSignal)
            });
            
            const data = await response.json();
            
            if (data.status === "success") {
                executeStatus.style.color = 'var(--success)';
                executeStatus.innerHTML = `✅ <b>Success!</b> ${data.message}`;
            } else {
                executeStatus.style.color = 'var(--danger)';
                executeStatus.innerHTML = `❌ <b>Failed:</b> ${data.message}`;
                executeBtn.disabled = false;
            }
        } catch (error) {
            executeStatus.style.color = 'var(--danger)';
            executeStatus.textContent = '❌ Failed to communicate with backend.';
            executeBtn.disabled = false;
        }
    });

    // Simple markdown parser for the mentor text since we requested markdown
    const marked = (text) => {
        if (!text) return text;
        return text
            .replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>')
            .replace(/\*(.*?)\*/g, '<em>$1</em>')
            .replace(/\n\n/g, '<br><br>')
            .replace(/\n/g, '<br>');
    };
});
