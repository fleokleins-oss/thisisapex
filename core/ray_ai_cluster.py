import ray
import torch
import torch.nn as nn
import numpy as np
from loguru import logger


@ray.remote(num_cpus=2)
class ApexAIAgent:
    """
    Ray Worker: Trains sweep prediction model on historical data.
    Runs on secondary compute node to avoid stealing latency
    from the trading loop.
    
    Model: 4-input MLP → probability that a detected sweep is real
    Training: Every 6 hours on rolling 30-day Qdrant data
    Inference: Called inline by ConfluenceGodMode (<2ms)
    """

    def __init__(self):
        self.model = nn.Sequential(
            nn.Linear(4, 32),
            nn.ReLU(),
            nn.Dropout(0.2),
            nn.Linear(32, 16),
            nn.ReLU(),
            nn.Linear(16, 1),
            nn.Sigmoid(),
        )
        self.optimizer = torch.optim.Adam(self.model.parameters(), lr=0.001)
        self.criterion = nn.MSELoss()

    def predict_sweep_prob(self, features: dict) -> float:
        """Fast inference called by the trading node."""
        self.model.eval()
        tensor = torch.tensor(
            [features["imbalance"], features["volume_delta"],
             features["refill"], features["oi_accel"]],
            dtype=torch.float32,
        )
        with torch.no_grad():
            return self.model(tensor).item()

    def train_on_historical_sweeps(self, data_vectors: list, labels: list) -> bool:
        """
        Background training on the secondary node.
        Called via Ray remote — does not block the trading loop.
        """
        logger.info("ML TRAINING started on compute node...")
        dataset = torch.tensor(data_vectors, dtype=torch.float32)
        lbls = torch.tensor(labels, dtype=torch.float32).unsqueeze(1)

        self.model.train()
        for epoch in range(10):
            self.optimizer.zero_grad()
            loss = self.criterion(self.model(dataset), lbls)
            loss.backward()
            self.optimizer.step()

        logger.success(f"ML TRAINING complete | Final loss: {loss.item():.4f}")
        return True
