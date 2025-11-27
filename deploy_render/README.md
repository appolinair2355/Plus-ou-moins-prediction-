# Bot de Prediction Telegram - Deploiement Render.com

## Configuration des Variables d'Environnement

Dans Render.com, configurez ces variables:

- `API_ID` : Votre ID API Telegram (depuis my.telegram.org)
- `API_HASH` : Votre Hash API Telegram
- `BOT_TOKEN` : Token du bot (depuis @BotFather)
- `ADMIN_ID` : Votre ID utilisateur Telegram
- `PORT` : 10000 (deja configure)
- `RENDER_DEPLOYMENT` : true

## Canaux configures

- Canal source (stats): -1002682552255
- Canal prediction: -1002617781803

## Deploiement

1. Creez un nouveau Web Service sur Render.com
2. Connectez votre repository ou uploadez les fichiers
3. Build Command: `pip install -r requirements.txt`
4. Start Command: `python main.py`
5. Configurez les variables d'environnement
