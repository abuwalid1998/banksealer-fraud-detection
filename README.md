# BankSealer Fraud Detection Framework

## Overview
This project implements a scalable, multi-layered fraud detection platform based on the **BankSealer** algorithmic framework. It is designed to analyze payment transactions in real-time, assigning anomaly scores by comparing individual behavior against historical patterns and peer-group clusters.

## Architecture
The system follows a microservices-oriented approach, separating the data ingestion (Spring Boot), the analytical engine (Python/ML), and the persistent storage (PostgreSQL).



## BankSealer Algorithm
The framework utilizes a three-pillar approach to ensure high precision and recall in detecting financial crimes:

1.  **Local Behavioral Profiling:** Uses **HBOS (Histogram-based Outlier Score)** to build a profile of a single user's historical habits.
2.  **Temporal Behavioral Profiling:** Monitors shifts in behavior over time using statistical thresholds to detect sudden velocity changes.
3.  **Global Behavioral Clustering:** Employs **DBSCAN** and **CBLOF** to group similar users, identifying outliers who deviate from their "peer" group.

## System Components
| Layer | Technology |
| :--- | :--- |
| **Backend** | Spring Boot (Java) |
| **ML Training** | Python (Scikit-learn, PyOD) |
| **Database** | PostgreSQL |
| **Dataset** | PaySim (Synthetic Mobile Money) |
| **Frontend** | HTML5 + Chart.js |
| **Containerization** | Docker & Docker Compose |
| **IaC** | Terraform |

## Dataset (PaySim)
We utilize the **PaySim** synthetic dataset, which simulates mobile money transactions based on real logs from an African financial service. It provides a rich environment for testing various fraud scenarios like "Cash-Out" and "Transfer" fraud.

## Running the System
1. **Clone the repository:**
   ```bash
   git clone [https://github.com/your-repo/banksealer-framework.git](https://github.com/your-repo/banksealer-framework.git)
