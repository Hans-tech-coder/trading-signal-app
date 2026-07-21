# Trading Signal App

This is a trading signal application that uses AI to scan the market, perform technical analysis using Gemini Vision on TradingView charts, and generate actionable trading signals (Buy, Sell, Hold).

## Overview

The application consists of a backend (Python/FastAPI) and a frontend (HTML/JS/CSS). The backend is responsible for:
1. Scanning major forex pairs to find the most volatile pair recently.
2. Automating the TradingView Desktop app using the TradingView MCP CLI to capture a screenshot of the chart.
3. Sending the captured chart screenshot to Google's Gemini Vision model for technical analysis.
4. Parsing the AI's response and sending it back to the frontend.

The frontend provides a user-friendly interface to trigger the scan and display the generated trading signal, including entry points, take profits, stop losses, and the reasoning behind the signal.

## Prerequisites

- **Python 3.8+**
- **TradingView Desktop App** (with TradingView MCP CLI configured)
- **Google Gemini API Key**
- **FRED API Key** (optional, depending on extensions)

## Setup and Installation

### 1. Clone the repository

```bash
git clone https://github.com/yourusername/trading-signal-app.git
cd trading-signal-app
```

### 2. Backend Setup

Navigate to the `backend` directory and install the required dependencies:

```bash
cd backend
pip install -r requirements.txt
```

Set up your environment variables by copying the example `.env` file:

```bash
cp .env.example .env
```

Open the `.env` file and insert your actual API keys:

```
GOOGLE_API_KEY=your_google_api_key_here
FRED_API_KEY=your_fred_api_key_here
```

### 3. Run the Application

Start the FastAPI backend server:

```bash
uvicorn main:app --reload
```

The backend API will be available at `http://127.0.0.1:8000`.

### 4. Open the Frontend

You can simply open the `frontend/index.html` file in your browser to interact with the application. No separate build step is required for the frontend.

## Disclaimer

This application is for educational purposes only. The generated trading signals should not be considered financial advice. Always do your own research before executing any trades.
