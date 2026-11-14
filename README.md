# Offramp

**Prove you can leave your frontier model.**

Offramp proves which of your app's LLM traffic can safely move from frontier models to open-weight models by replaying real requests through [Otari](https://otari.ai) and turning the statistical evidence into a ready-to-deploy routing policy.

**🌐 Website: https://offramp-tau.vercel.app** · live, interactive evidence from the real Otari runs.

The project lives on the [`otari-hackathon`](https://github.com/hari-hara-sudharsan/Mozilla/tree/otari-hackathon) branch, built for MozFest's Developers Wilding: Online hackathon.

```bash
git clone -b otari-hackathon https://github.com/hari-hara-sudharsan/Mozilla.git
cd Mozilla
uv run offramp demo --out runs/demo
```

The demo runs offline with fictional models, so it needs no key and costs nothing.
