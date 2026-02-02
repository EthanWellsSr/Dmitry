# Dmitry

Dmitry is a Python-based automated trading system built to explore real-time decision logic, API integration, and system reliability.  
The project focuses on software architecture, automation, logging, and fault tolerance rather than trading performance.

> **Note:** This project is for educational and experimental purposes only.

---

## Overview

Dmitry is designed as a continuously running service that:
- Interfaces with third-party exchange APIs
- Operates in both **simulation** and **live** modes
- Executes decisions based on configurable thresholds
- Logs all activity for later analysis and verification
- Sends automated alerts and health notifications

The primary goal of the project is to practice building **maintainable, testable, and resilient software systems**.

---

## Key Features

- **Python-based architecture**
- **Simulation mode** for safe testing and validation
- **Live mode** using real account balances
- **Automated logging** to Google Sheets for traceability
- **Email alerts** for trade execution, errors, and system heartbeat
- **Graceful error handling** and crash recovery logic
- **Config-driven behavior** (no hardcoded parameters)

---

## System Architecture

- Core logic written in Python
- External API integration for market data and execution
- Separation of concerns between:
  - Decision logic
  - Execution layer
  - Logging and notifications
- Designed to run unattended for extended periods

---

## Why This Project Exists

This project was built to strengthen skills in:
- API integration
- State management
- Automation and monitoring
- Defensive programming
- Long-running service design

It is **not** intended as a financial product or recommendation.

---

## Status

Active personal project.  
Continuously refactored and improved as part of ongoing software development practice.

---

## Disclaimer

This project is provided as-is for educational purposes only.  
No financial advice is given or implied.

