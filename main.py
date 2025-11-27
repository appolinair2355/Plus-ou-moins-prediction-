import os
import asyncio
import re
import json
import zipfile
import tempfile
import shutil
import glob
from datetime import datetime, timedelta
from telethon import TelegramClient, events
from telethon.events import ChatAction
from dotenv import load_dotenv
from predictor import CardPredictor
from yaml_manager import init_database, db
from excel_importer import ExcelPredictionManager
from aiohttp import web
import threading

# Load environment variables
load_dotenv()

# --- CONFIGURATION ---
try:
    API_ID = int(os.getenv('API_ID') or '0')
    API_HASH = os.getenv('API_HASH') or ''
    BOT_TOKEN = os.getenv('BOT_TOKEN') or ''
    ADMIN_ID = int(os.getenv('ADMIN_ID') or '0') if os.getenv('ADMIN_ID') else None
    PORT = int(os.getenv('PORT') or '5000')
    DISPLAY_CHANNEL = int(os.getenv('DISPLAY_CHANNEL') or '-1002999811353')

    # Validation des variables requises
    if not API_ID or API_ID == 0:
        raise ValueError("API_ID manquant ou invalide")
    if not API_HASH:
        raise ValueError("API_HASH manquant")
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN manquant")

    print(f"✅ Configuration chargée: API_ID={API_ID}, ADMIN_ID={ADMIN_ID or 'Non configuré'}, PORT={PORT}, DISPLAY_CHANNEL={DISPLAY_CHANNEL}")
except Exception as e:
    print(f"❌ Erreur configuration: {e}")
    print("Vérifiez vos variables d'environnement")
    exit(1)

# Fichier de configuration persistante
CONFIG_FILE = 'bot_config.json'

# Variables d'état
detected_stat_channel = None
detected_display_channel = None
confirmation_pending = {}
prediction_interval = 5  # Intervalle en minutes

# Variables pour la détection automatique des fichiers Excel
EXCEL_WATCH_DIR = "."  # Répertoire à surveiller
processed_excel_files = set()  # Fichiers déjà traités
last_excel_check = None  # Dernière vérification

def load_config():
    """Load configuration with priority: JSON > Database > Environment"""
    global detected_stat_channel, detected_display_channel, prediction_interval
    try:
        # Toujours essayer JSON en premier (source de vérité)
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                config = json.load(f)
                detected_stat_channel = config.get('stat_channel')
                detected_display_channel = config.get('display_channel', DISPLAY_CHANNEL)
                prediction_interval = config.get('prediction_interval', 1)
                print(f"✅ Configuration chargée depuis JSON: Stats={detected_stat_channel}, Display={detected_display_channel}, Intervalle={prediction_interval}min")
                return

        # Fallback sur base de données si JSON n'existe pas
        if db:
            detected_stat_channel = db.get_config('stat_channel')
            detected_display_channel = db.get_config('display_channel') or DISPLAY_CHANNEL
            interval_config = db.get_config('prediction_interval')
            if detected_stat_channel:
                detected_stat_channel = int(detected_stat_channel)
            if detected_display_channel:
                detected_display_channel = int(detected_display_channel)
            if interval_config:
                prediction_interval = int(interval_config)
            print(f"✅ Configuration chargée depuis la DB: Stats={detected_stat_channel}, Display={detected_display_channel}, Intervalle={prediction_interval}min")
        else:
            # Utiliser le canal de display par défaut depuis les variables d'environnement
            detected_display_channel = DISPLAY_CHANNEL
            prediction_interval = 1
            print(f"ℹ️ Configuration par défaut: Display={detected_display_channel}, Intervalle={prediction_interval}min")
    except Exception as e:
        print(f"⚠️ Erreur chargement configuration: {e}")
        # Valeurs par défaut en cas d'erreur
        detected_stat_channel = None
        detected_display_channel = DISPLAY_CHANNEL
        prediction_interval = 1

def save_config():
    """Save configuration to database and JSON backup"""
    try:
        if db:
            # Sauvegarde en base de données
            db.set_config('stat_channel', detected_stat_channel)
            db.set_config('display_channel', detected_display_channel)
            db.set_config('prediction_interval', prediction_interval)
            print("💾 Configuration sauvegardée en base de données")

        # Sauvegarde JSON de secours
        config = {
            'stat_channel': detected_stat_channel,
            'display_channel': detected_display_channel,
            'prediction_interval': prediction_interval
        }
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=2)
        print(f"💾 Configuration sauvegardée: Stats={detected_stat_channel}, Display={detected_display_channel}, Intervalle={prediction_interval}min")
    except Exception as e:
        print(f"❌ Erreur sauvegarde configuration: {e}")

def update_channel_config(source_id: int, target_id: int):
    """Update channel configuration"""
    global detected_stat_channel, detected_display_channel
    detected_stat_channel = source_id
    detected_display_channel = target_id
    save_config()

# Initialize database
database = init_database()

# Gestionnaire de prédictions
predictor = CardPredictor()

# Gestionnaire d'importation Excel
excel_manager = ExcelPredictionManager()

# Initialize Telegram client with unique session name
import time
session_name = f'bot_session_{int(time.time())}'
client = TelegramClient(session_name, API_ID, API_HASH)

async def start_bot():
    """Start the bot with proper error handling"""
    try:
        # Load saved configuration first
        load_config()

        await client.start(bot_token=BOT_TOKEN)
        print("Bot démarré avec succès...")

        # Get bot info
        me = await client.get_me()
        username = getattr(me, 'username', 'Unknown') or f"ID:{getattr(me, 'id', 'Unknown')}"
        print(f"Bot connecté: @{username}")

    except Exception as e:
        print(f"Erreur lors du démarrage du bot: {e}")
        return False

    return True

# --- INVITATION / CONFIRMATION ---
@client.on(events.ChatAction())
async def handler_join(event):
    """Handle bot joining channels/groups"""
    global confirmation_pending

    try:
        # Ignorer les événements d'épinglage de messages
        if event.new_pin or event.unpin:
            return

        # Ignorer les événements sans user_id (comme les épinglages)
        if not event.user_id:
            return

        print(f"ChatAction event: {event}")
        print(f"user_joined: {event.user_joined}, user_added: {event.user_added}")
        print(f"user_id: {event.user_id}, chat_id: {event.chat_id}")

        if event.user_joined or event.user_added:
            me = await client.get_me()
            me_id = getattr(me, 'id', None)
            print(f"Mon ID: {me_id}, Event user_id: {event.user_id}")

            if event.user_id == me_id:
                confirmation_pending[event.chat_id] = 'waiting_confirmation'

                # Get channel info
                try:
                    chat = await client.get_entity(event.chat_id)
                    chat_title = getattr(chat, 'title', f'Canal {event.chat_id}')
                except:
                    chat_title = f'Canal {event.chat_id}'

                # Send private invitation to admin
                invitation_msg = f"""🔔 **Nouveau canal détecté**

📋 **Canal** : {chat_title}
🆔 **ID** : {event.chat_id}

**Choisissez le type de canal** :
• `/set_stat {event.chat_id}` - Canal de statistiques
• `/set_display {event.chat_id}` - Canal de diffusion

Envoyez votre choix en réponse à ce message."""

                try:
                    await client.send_message(ADMIN_ID, invitation_msg)
                    print(f"Invitation envoyée à l'admin pour le canal: {chat_title} ({event.chat_id})")
                except Exception as e:
                    print(f"Erreur envoi invitation privée: {e}")
                    # Fallback: send to the channel temporarily for testing
                    await client.send_message(event.chat_id, f"⚠️ Impossible d'envoyer l'invitation privée. Canal ID: {event.chat_id}")
                    print(f"Message fallback envoyé dans le canal {event.chat_id}")
    except Exception as e:
        print(f"Erreur dans handler_join: {e}")

@client.on(events.NewMessage(pattern=r'/set_stat (-?\d+)'))
async def set_stat_channel(event):
    """Set statistics channel (only admin in private)"""
    global detected_stat_channel, confirmation_pending

    try:
        # Only allow in private chat with admin
        if event.is_group or event.is_channel:
            return

        if ADMIN_ID and event.sender_id != ADMIN_ID:
            await event.respond("❌ Seul l'administrateur peut configurer les canaux")
            return

        # Extract channel ID from command
        match = event.pattern_match
        channel_id = int(match.group(1))

        # Check if channel is waiting for confirmation
        if channel_id not in confirmation_pending:
            await event.respond("❌ Ce canal n'est pas en attente de configuration")
            return

        detected_stat_channel = channel_id
        confirmation_pending[channel_id] = 'configured_stat'

        # Save configuration
        save_config()

        try:
            chat = await client.get_entity(channel_id)
            chat_title = getattr(chat, 'title', f'Canal {channel_id}')
        except:
            chat_title = f'Canal {channel_id}'

        await event.respond(f"✅ **Canal de statistiques configuré**\n📋 {chat_title}\n\n✨ Le bot surveillera ce canal pour les prédictions - développé par Sossou Kouamé Appolinaire\n💾 Configuration sauvegardée automatiquement")
        print(f"Canal de statistiques configuré: {channel_id}")

    except Exception as e:
        print(f"Erreur dans set_stat_channel: {e}")

@client.on(events.NewMessage(pattern=r'/force_set_stat (-?\d+)'))
async def force_set_stat_channel(event):
    """Force set statistics channel without waiting for invitation (admin only)"""
    global detected_stat_channel

    try:
        # Only allow admin
        if ADMIN_ID and event.sender_id != ADMIN_ID:
            await event.respond("❌ Seul l'administrateur peut configurer les canaux")
            return

        # Extract channel ID from command
        match = event.pattern_match
        channel_id = int(match.group(1))

        detected_stat_channel = channel_id

        # Save configuration
        save_config()

        try:
            chat = await client.get_entity(channel_id)
            chat_title = getattr(chat, 'title', f'Canal {channel_id}')
        except:
            chat_title = f'Canal {channel_id}'

        await event.respond(f"✅ **Canal de statistiques configuré (force)**\n📋 {chat_title}\n🆔 ID: {channel_id}\n\n✨ Le bot surveillera ce canal pour les prédictions\n💾 Configuration sauvegardée automatiquement")
        print(f"Canal de statistiques configuré (force): {channel_id}")

    except Exception as e:
        print(f"Erreur dans force_set_stat_channel: {e}")
        await event.respond(f"❌ Erreur: {e}")

@client.on(events.NewMessage(pattern=r'/set_display (-?\d+)'))
async def set_display_channel(event):
    """Set display channel (only admin in private)"""
    global detected_display_channel, confirmation_pending

    try:
        # Only allow in private chat with admin
        if event.is_group or event.is_channel:
            return

        if event.sender_id != ADMIN_ID:
            await event.respond("❌ Seul l'administrateur peut configurer les canaux")
            return

        # Extract channel ID from command
        match = event.pattern_match
        channel_id = int(match.group(1))

        # Check if channel is waiting for confirmation
        if channel_id not in confirmation_pending:
            await event.respond("❌ Ce canal n'est pas en attente de configuration")
            return

        detected_display_channel = channel_id
        confirmation_pending[channel_id] = 'configured_display'

        # Save configuration
        save_config()

        try:
            chat = await client.get_entity(channel_id)
            chat_title = getattr(chat, 'title', f'Canal {channel_id}')
        except:
            chat_title = f'Canal {channel_id}'

        await event.respond(f"✅ **Canal de diffusion configuré**\n📋 {chat_title}\n\n🚀 Le bot publiera les prédictions dans ce canal - développé par Sossou Kouamé Appolinaire\n💾 Configuration sauvegardée automatiquement")
        print(f"Canal de diffusion configuré: {channel_id}")

    except Exception as e:
        print(f"Erreur dans set_display_channel: {e}")

@client.on(events.NewMessage(pattern=r'/force_set_display (-?\d+)'))
async def force_set_display_channel(event):
    """Force set display channel without waiting for invitation (admin only)"""
    global detected_display_channel

    try:
        # Only allow admin
        if ADMIN_ID and event.sender_id != ADMIN_ID:
            await event.respond("❌ Seul l'administrateur peut configurer les canaux")
            return

        # Extract channel ID from command
        match = event.pattern_match
        channel_id = int(match.group(1))

        detected_display_channel = channel_id

        # Save configuration
        save_config()

        try:
            chat = await client.get_entity(channel_id)
            chat_title = getattr(chat, 'title', f'Canal {channel_id}')
        except:
            chat_title = f'Canal {channel_id}'

        await event.respond(f"✅ **Canal de diffusion configuré (force)**\n📋 {chat_title}\n🆔 ID: {channel_id}\n\n🚀 Le bot publiera les prédictions dans ce canal\n💾 Configuration sauvegardée automatiquement")
        print(f"Canal de diffusion configuré (force): {channel_id}")

    except Exception as e:
        print(f"Erreur dans force_set_display_channel: {e}")
        await event.respond(f"❌ Erreur: {e}")


async def verify_excel_predictions(game_number: int, message_text: str):
    """Fonction consolidée pour vérifier toutes les prédictions Excel en attente"""
    for key, pred in list(excel_manager.predictions.items()):
        # Ignorer si pas lancée ou déjà vérifiée
        if not pred["launched"] or pred.get("verified", False):
            continue

        pred_numero = pred["numero"]
        expected_winner = pred["victoire"]
        current_offset = pred.get("current_offset", 0)
        target_number = pred_numero + current_offset

        # DÉTECTION DE SAUT DE NUMÉRO
        if game_number > target_number:
            print(f"⚠️ Numéro sauté: #{pred_numero} attendait #{target_number}, reçu #{game_number}")

            while current_offset <= 2 and game_number > pred_numero + current_offset:
                current_offset += 1
                print(f"⏭️ Prédiction #{pred_numero}: saut à offset {current_offset}")

            # Note: excel_manager.verify_excel_prediction gère maintenant la vérification d'échec > 2
            if current_offset > 2:
                # Marquer comme échec si l'offset dépasse 2
                await update_prediction_status(pred, pred_numero, expected_winner, "❌", True) # MODIFIÉ : "⭕✍🏻" -> "❌"
                continue
            else:
                pred["current_offset"] = current_offset
                excel_manager.save_predictions()

        # Vérification séquentielle
        status, should_continue = excel_manager.verify_excel_prediction(
            game_number, message_text, pred_numero, expected_winner, current_offset
        )

        if status:
            await update_prediction_status(pred, pred_numero, expected_winner, status, True)
        elif should_continue and game_number == pred_numero + current_offset:
            new_offset = current_offset + 1
            if new_offset <= 2:
                pred["current_offset"] = new_offset
                excel_manager.save_predictions()
                print(f"⏭️ Prédiction #{pred_numero}: offset {new_offset}")
            else:
                # Échec définitif après offset 2 non réussi
                await update_prediction_status(pred, pred_numero, expected_winner, "❌", True) # MODIFIÉ : "⭕✍🏻" -> "❌"

async def update_prediction_status(pred: dict, numero: int, winner: str, status: str, verified: bool):
    """Mise à jour unifiée du statut de prédiction"""
    msg_id = pred.get("message_id")
    channel_id = pred.get("channel_id")

    if msg_id and channel_id:
        # Utiliser la nouvelle fonction (qui prend numero et winner) pour obtenir le format complet (incluant statut :⏳)
        full_base_text_with_placeholder = excel_manager.get_prediction_format(numero, winner)

        # Le format complet est: 🔵{numero}:🅿️+6,5🔵statut :⏳
        # Nous devons remplacer la fin :⏳ par :{status}

        # Sépare le texte avant 'statut :⏳' et prend la première partie
        base_format = full_base_text_with_placeholder.rsplit("statut :⏳", 1)[0]

        # Reconstruit le message avec le nouveau statut
        new_text = f"{base_format}statut :{status}"

        try:
            await client.edit_message(channel_id, msg_id, new_text)
            pred["verified"] = verified
            excel_manager.save_predictions()
            print(f"✅ Prédiction #{numero} mise à jour: {status}")
        except Exception as e:
            print(f"❌ Erreur mise à jour #{numero}: {e}")


# --- COMMANDES DE BASE ---
@client.on(events.NewMessage(pattern='/start'))
async def start_command(event):
    """Send welcome message when user starts the bot"""
    try:
        welcome_msg = """🎯 **Bot de Prédiction de Cartes - Bienvenue !**

🔹 **Développé par Sossou Kouamé Appolinaire**

**Fonctionnalités** :
• 📊 Import de prédictions depuis fichier Excel
• 🔍 Surveillance automatique du canal source
• 🎯 Lancement des prédictions basé sur le fichier Excel
• ✅ Vérification des résultats avec offsets (0, 1, 2)

**Configuration** :
1. Ajoutez-moi dans vos canaux
2. Je vous enverrai automatiquement une invitation privée
3. Répondez avec `/set_stat [ID]` ou `/set_display [ID]`
4. Envoyez votre fichier Excel (.xlsx) pour importer les prédictions

**Commandes Admin** :
• `/start` - Ce message
• `/status` - État du bot
• `/sta` - Statistiques Excel
• `/excel_clear` - Effacer les prédictions Excel
• `/reset` - Réinitialiser toutes les données
• `/deploy` - Créer package de déploiement (zip)
• `/ni` - Informations système
• `/set_stat [ID]` - Configurer canal source
• `/set_display [ID]` - Configurer canal diffusion
• `/force_set_stat [ID]` - Forcer config canal source
• `/force_set_display [ID]` - Forcer config canal diffusion

**Format Excel** :
Le fichier doit contenir 3 colonnes :
• Date & Heure
• Numéro (ex: 881, 886, 891...)
• Victoire (Joueur ou Banquier)

**Format de prédiction** :
• Joueur (P+6,5) : 🔵XXX:🅿️+6,5🔵statut :⏳
• Banquier (M-4,5) : 🔵XXX:Ⓜ️-4,,5🔵statut :⏳

Le bot est prêt à analyser vos jeux ! 🚀"""

        await event.respond(welcome_msg)
        print(f"Message de bienvenue envoyé à l'utilisateur {event.sender_id}")

        # Test message private pour vérifier la connectivité
        if event.sender_id == ADMIN_ID:
            await asyncio.sleep(2)
            test_msg = "🔧 Test de connectivité : Je peux vous envoyer des messages privés !"
            await event.respond(test_msg)

    except Exception as e:
        print(f"Erreur dans start_command: {e}")

# --- COMMANDES ADMINISTRATIVES ---
@client.on(events.NewMessage(pattern='/status'))
async def show_status(event):
    """Show bot status (admin only)"""
    try:
        # Permettre si ADMIN_ID est configuré ou en mode développement
        if ADMIN_ID and event.sender_id != ADMIN_ID:
            return

        # Recharger la configuration pour éviter les valeurs obsolètes
        load_config()

        config_status = "✅ Sauvegardée" if os.path.exists(CONFIG_FILE) else "❌ Non sauvegardée"
        status_msg = f"""📊 **Statut du Bot**

Canal statistiques: {'✅ Configuré' if detected_stat_channel else '❌ Non configuré'} ({detected_stat_channel})
Canal diffusion: {'✅ Configuré' if detected_display_channel else '❌ Non configuré'} ({detected_display_channel})
⏱️ Intervalle de prédiction: {prediction_interval} minutes
Configuration persistante: {config_status}
Prédictions actives: {len(predictor.prediction_status)}
Dernières prédictions: {len(predictor.last_predictions)}
"""
        await event.respond(status_msg)
    except Exception as e:
        print(f"Erreur dans show_status: {e}")

@client.on(events.NewMessage(pattern='/reset'))
async def reset_data(event):
    """Réinitialisation des données (admin uniquement)"""
    try:
        if event.sender_id != ADMIN_ID:
            return

        # Réinitialiser les données du predictor
        predictor.reset()

        # Réinitialiser les données YAML
        db.reset_all_data()

        msg = """🔄 **Données réinitialisées avec succès !**

✅ Prédictions en attente: vidées
✅ Base de données YAML: réinitialisée
✅ Configuration: préservée

Le bot est prêt pour un nouveau cycle."""

        await event.respond(msg)
        print(f"Données réinitialisées par l'admin")

    except Exception as e:
        print(f"Erreur dans reset_data: {e}")
        await event.respond(f"❌ Erreur lors de la réinitialisation: {e}")

@client.on(events.NewMessage(pattern='/ni'))
async def ni_command(event):
    """Commande /ni - Informations sur le système de prédiction"""
    try:
        # Utiliser les variables globales configurées
        stats_channel = detected_stat_channel or 'Non configuré'
        display_channel = detected_display_channel or 'Non configuré'

        # Compter les prédictions actives depuis le predictor
        active_predictions = len([s for s in predictor.prediction_status.values() if s == '⌛'])

        msg = f"""🎯 **Système de Prédiction NI - Statut**

📊 **Configuration actuelle**:
• Canal source: {stats_channel}
• Canal affichage: {display_channel}
• Prédictions Excel actives: {active_predictions}
• Intervalle: {prediction_interval} minute(s)

🎮 **Fonctionnalités**:
• Prédictions basées uniquement sur fichier Excel
• Vérification séquentielle avec offsets 0→1→2
• Format Joueur: "🔵XXX:🅿️+6,5🔵statut :⏳"
• Format Banquier: "🔵XXX:Ⓜ️-4,,5🔵statut :⏳"

🔧 **Commandes disponibles**:
• `/set_stat [ID]` - Configurer canal source
• `/set_display [ID]` - Configurer canal affichage
• `/excel_status` - Voir prédictions Excel
• `/reset` - Réinitialiser les données
• `/deploy` - Créer package de déploiement

✅ **Bot opérationnel** - Version 2025"""

        await event.respond(msg)
        print(f"Commande /ni exécutée par {event.sender_id}")

    except Exception as e:
        print(f"Erreur dans ni_command: {e}")
        await event.respond(f"❌ Erreur: {e}")

@client.on(events.NewMessage(pattern='/deploy'))
async def deploy_command(event):
    """Créer un package zip de déploiement avec Python zipfile"""
    try:
        if ADMIN_ID and event.sender_id != ADMIN_ID:
            await event.respond("❌ Seul l'administrateur peut créer un package de déploiement")
            return

        await event.respond("📦 **Création du package en cours...**")

        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        zip_filename = f"bien233_{timestamp}.zip"

        files_to_include = [
            "main.py",
            "predictor.py",
            "excel_importer.py",
            "yaml_manager.py",
            "requirements.txt",
            "bot_config.json",
            ".gitignore"
        ]

        # Créer le zip avec Python
        try:
            with zipfile.ZipFile(zip_filename, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as zipf:
                # Ajouter le dossier deploy_render
                deploy_dir = "deploy_render"
                if os.path.exists(deploy_dir):
                    for root, dirs, files in os.walk(deploy_dir):
                        for file in files:
                            file_path = os.path.join(root, file)
                            arcname = os.path.relpath(file_path, ".")
                            zipf.write(file_path, arcname)
                            print(f"  ✓ Ajouté: {arcname}")

                # Ajouter les fichiers principaux
                for filename in files_to_include:
                    if os.path.exists(filename):
                        zipf.write(filename)
                        print(f"  ✓ Ajouté: {filename}")

            file_size = os.path.getsize(zip_filename)
            size_mb = file_size / (1024 * 1024)

            await event.respond(f"✅ **Package créé: {size_mb:.2f} MB**\n📤 Envoi en cours...")

            # Envoyer le fichier
            await client.send_file(
                event.chat_id,
                zip_filename,
                caption=f"📦 **Package bien233 créé avec succès!**\n\n✅ Fichier: {zip_filename}\n💾 Taille: {size_mb:.2f} MB\n🚀 Prêt pour déploiement Replit!"
            )

            # Nettoyer
            try:
                os.remove(zip_filename)
                print(f"🗑️ Fichier temporaire supprimé")
            except Exception as cleanup_error:
                print(f"⚠️ Impossible de supprimer le fichier: {cleanup_error}")

            print(f"✅ Package {zip_filename} créé et envoyé avec succès")

        except Exception as zip_error:
            await event.respond(f"❌ Erreur création zip: {zip_error}")
            print(f"❌ Erreur zipfile: {zip_error}")

    except Exception as e:
        print(f"❌ Erreur deploy_command: {e}")
        await event.respond(f"❌ Erreur: {e}")


@client.on(events.NewMessage(pattern='/test_invite'))
async def test_invite(event):
    """Test sending invitation (admin only)"""
    try:
        if event.sender_id != ADMIN_ID:
            return

        # Test invitation message
        test_msg = f"""🔔 **Test d'invitation**

📋 **Canal test** : Canal de test
🆔 **ID** : -1001234567890

**Choisissez le type de canal** :
• `/set_stat -1001234567890` - Canal de statistiques
• `/set_display -1001234567890` - Canal de diffusion

Ceci est un message de test pour vérifier les invitations."""

        await event.respond(test_msg)
        print(f"Message de test envoyé à l'admin")

    except Exception as e:
        print(f"Erreur dans test_invite: {e}")

@client.on(events.NewMessage(pattern='/sta'))
async def show_excel_stats(event):
    """Show Excel predictions statistics"""
    try:
        if ADMIN_ID and event.sender_id != ADMIN_ID:
            return

        # Recharger la configuration pour éviter les valeurs obsolètes
        load_config()

        stats = excel_manager.get_stats()

        msg = f"""📊 **Statut des Prédictions Excel**

📋 **Statistiques Excel**:
• Total prédictions: {stats['total']}
• En attente: {stats['pending']}
• Lancées: {stats['launched']}

📈 **Configuration actuelle**:
• Canal stats configuré: {'✅' if detected_stat_channel else '❌'} ({detected_stat_channel or 'Aucun'})
• Canal affichage configuré: {'✅' if detected_display_channel else '❌'} ({detected_display_channel or 'Aucun'})

🔧 **Format de prédiction**:
• Joueur (P+6,5) : 🔵XXX:🅿️+6,5🔵statut :⏳
• Banquier (M-4,5) : 🔵XXX:Ⓜ️-4,,5🔵statut :⏳

✅ Prédictions uniquement depuis fichier Excel"""

        await event.respond(msg)
        print(f"Statut Excel envoyé à l'admin")

    except Exception as e:
        print(f"Erreur dans show_excel_stats: {e}")
        await event.respond(f"❌ Erreur: {e}")

@client.on(events.NewMessage(pattern='/excel_clear'))
async def clear_excel_predictions(event):
    """Effacer toutes les prédictions Excel"""
    try:
        if ADMIN_ID and event.sender_id != ADMIN_ID:
            return

        old_count = len(excel_manager.predictions)
        excel_manager.predictions.clear()
        excel_manager.save_predictions()

        msg = f"""🗑️ **Prédictions Excel effacées**

✅ {old_count} prédictions supprimées
📋 La base est maintenant vide

Vous pouvez importer un nouveau fichier Excel."""

        await event.respond(msg)
        print(f"Prédictions Excel effacées par l'admin: {old_count} entrées")

    except Exception as e:
        print(f"Erreur dans clear_excel_predictions: {e}")
        await event.respond(f"❌ Erreur: {e}")

# Commande /report et /scheduler supprimées (non utilisées)

@client.on(events.NewMessage(func=lambda e: e.is_private and e.document))
async def handle_excel_document(event):
    """Détecte automatiquement les fichiers Excel envoyés par l'admin (sans commande)"""
    try:
        if ADMIN_ID and event.sender_id != ADMIN_ID:
            return

        if not event.message.file:
            return

        mime_type = event.message.file.mime_type or ""
        file_name = event.message.file.name or ""

        excel_mimes = [
            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            'application/vnd.ms-excel',
            'application/octet-stream'
        ]
        excel_extensions = ['.xlsx', '.xls']

        is_excel = any(mime in mime_type for mime in excel_mimes) or any(file_name.lower().endswith(ext) for ext in excel_extensions)

        if not is_excel:
            return

        print(f"📥 Fichier Excel détecté via Telegram: {file_name}")
        await event.respond("📥 **Fichier Excel détecté! Téléchargement en cours...**")

        file_path = await event.message.download_media()

        if not file_path:
            await event.respond("❌ **Erreur**: Impossible de télécharger le fichier.")
            return

        await event.respond("⚙️ **Importation des prédictions...**")

        old_count = len(excel_manager.predictions)
        result = excel_manager.import_excel(file_path, replace_mode=True)

        try:
            os.remove(file_path)
        except:
            pass

        if result["success"]:
            stats = excel_manager.get_stats()
            consecutive_info = result.get('consecutive_skipped', 0)

            msg = f"""📥 Import Excel via Telegram

✅ Fichier Excel importé avec succès!
• Prédictions importées: {result['imported']}
• Anciennes remplacées: {old_count}
• Consécutifs ignorés: {consecutive_info}
• Total en base: {stats['total']}

Le système est prêt pour les prédictions! 🎉

📋 **Statistiques**:
• En attente: {stats['pending']}
• Lancées: {stats['launched']}"""

            await event.respond(msg)
            print(f"✅ Import Excel via Telegram réussi: {result['imported']} prédictions")
        else:
            await event.respond(f"❌ **Erreur importation Excel**: {result.get('error', 'Erreur inconnue')}")
            print(f"❌ Erreur importation Excel: {result.get('error')}")

    except Exception as e:
        print(f"Erreur dans handle_excel_document: {e}")
        await event.respond(f"❌ **Erreur critique**: {e}")

@client.on(events.NewMessage(pattern=r'/upload_excel', func=lambda e: e.is_private and e.sender_id == ADMIN_ID and e.media))
async def handle_excel_upload(event):
    """Handle Excel file upload from admin in private chat (legacy command)"""
    pass
        # --- LOGIQUE PRINCIPALE : ÉCOUTE DU CANAL SOURCE ---

@client.on(events.NewMessage())
async def handle_new_message(event):
    """Gère les nouveaux messages dans le canal de statistiques (source)"""
    if not detected_stat_channel:
        return
    if not (event.is_channel and event.chat_id == detected_stat_channel):
        return
    if True:
        message_text = event.raw_text
        game_number = predictor.extract_game_number(message_text)

        if game_number:
            # --- ÉTAPE 1: LANCEMENT DE LA PRÉDICTION EXCEL ---
            if detected_display_channel and detected_stat_channel:
                # Trouver la prédiction la plus proche (dans la tolérance)
                closest_pred = excel_manager.find_close_prediction(game_number)

                if closest_pred:
                    pred_key = closest_pred["key"]
                    pred_numero = closest_pred["prediction"]["numero"]
                    pred_victoire = closest_pred["prediction"]["victoire"]

                    # Création du message avec le nouveau format (ex: 🔵XXX:🅿️+6,5🔵statut :⏳)
                    prediction_text = excel_manager.get_prediction_format(pred_numero, pred_victoire)

                    try:
                        sent_message = await client.send_message(detected_display_channel, prediction_text)
                        # Marquer comme lancé et enregistrer l'ID du message
                        excel_manager.mark_as_launched(pred_key, sent_message.id, detected_display_channel)
                        ecart = pred_numero - game_number
                        print(f"✅ Prédiction Excel lancée: {prediction_text} | Canal source: #{game_number} (écart: +{ecart} parties)")
                    except Exception as e:
                        print(f"❌ Erreur envoi prédiction Excel: {e}")
                else:
                    print(f"ℹ️ Aucune prédiction à lancer pour le jeu #{game_number}")
            else:
                if not detected_display_channel:
                    print(f"⚠️ Canal de diffusion non configuré - impossible de lancer des prédictions")
                if not detected_stat_channel:
                    print(f"⚠️ Canal de statistiques non configuré")

            # --- ÉTAPE 2: VÉRIFICATION DES PRÉDICTIONS EXCEL LANCÉES ---
            await verify_excel_predictions(game_number, message_text)

# --- DÉTECTION AUTOMATIQUE DES FICHIERS EXCEL ---

def get_excel_files_in_project():
    """Retourne la liste des fichiers Excel dans le répertoire du projet"""
    excel_patterns = ["*.xlsx", "*.xls"]
    excel_files = []
    for pattern in excel_patterns:
        excel_files.extend(glob.glob(os.path.join(EXCEL_WATCH_DIR, pattern)))
    return excel_files

def load_processed_files():
    """Charge la liste des fichiers déjà traités depuis un fichier de persistance"""
    global processed_excel_files
    try:
        processed_file = "processed_excel_files.json"
        if os.path.exists(processed_file):
            with open(processed_file, 'r') as f:
                data = json.load(f)
                processed_excel_files = set(data.get('files', []))
    except Exception as e:
        print(f"⚠️ Erreur chargement fichiers traités: {e}")
        processed_excel_files = set()

def save_processed_files():
    """Sauvegarde la liste des fichiers traités"""
    try:
        processed_file = "processed_excel_files.json"
        with open(processed_file, 'w') as f:
            json.dump({'files': list(processed_excel_files)}, f)
    except Exception as e:
        print(f"⚠️ Erreur sauvegarde fichiers traités: {e}")

async def check_new_excel_files():
    """Vérifie s'il y a de nouveaux fichiers Excel dans le projet"""
    global processed_excel_files

    try:
        current_files = get_excel_files_in_project()

        for file_path in current_files:
            file_name = os.path.basename(file_path)
            file_mtime = os.path.getmtime(file_path)
            file_key = f"{file_name}_{file_mtime}"

            if file_key not in processed_excel_files:
                print(f"📥 Nouveau fichier Excel détecté: {file_name}")
                await auto_import_excel(file_path)
                processed_excel_files.add(file_key)
                save_processed_files()

    except Exception as e:
        print(f"⚠️ Erreur vérification fichiers Excel: {e}")

async def auto_import_excel(file_path: str):
    """Importe automatiquement un fichier Excel et envoie la confirmation à l'admin"""
    try:
        file_name = os.path.basename(file_path)
        print(f"📥 Import Automatique: {file_name}")

        old_count = len(excel_manager.predictions)
        result = excel_manager.import_excel(file_path, replace_mode=True)

        if result["success"]:
            stats = excel_manager.get_stats()
            consecutive_info = result.get('consecutive_skipped', 0)

            msg = f"""📥 Import Automatique dans Projet

✅ Fichier Excel importé avec succès!
• Prédictions importées: {result['imported']}
• Anciennes remplacées: {old_count}
• Consécutifs ignorés: {consecutive_info}
• Total en base: {stats['total']}

Le système est prêt pour la nouvelle journée! 🎉"""

            print(msg)

            if ADMIN_ID:
                try:
                    await client.send_message(ADMIN_ID, msg)
                    print(f"✅ Message de confirmation envoyé à l'admin")
                except Exception as e:
                    print(f"⚠️ Impossible d'envoyer le message à l'admin: {e}")
        else:
            error_msg = f"❌ Erreur import Excel automatique: {result.get('error', 'Erreur inconnue')}"
            print(error_msg)
            if ADMIN_ID:
                try:
                    await client.send_message(ADMIN_ID, error_msg)
                except:
                    pass

    except Exception as e:
        print(f"❌ Erreur import automatique: {e}")

async def excel_file_watcher():
    """Boucle de surveillance des fichiers Excel (toutes les 10 secondes)"""
    load_processed_files()
    print("👀 Surveillance des fichiers Excel activée")

    while True:
        try:
            await check_new_excel_files()
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"⚠️ Erreur dans le watcher Excel: {e}")
            await asyncio.sleep(30)

# --- FONCTIONS UTILITAIRES POUR LE SERVEUR WEB ---

async def health_check(request):
    """Simple health check endpoint"""
    return web.Response(text="Bot is running", status=200)

async def bot_status(request):
    """Status endpoint for the bot"""
    stats = excel_manager.get_stats()
    status = {
        'status': 'Running',
        'stat_channel': detected_stat_channel,
        'display_channel': detected_display_channel,
        'excel_predictions': stats
    }
    return web.json_response(status)

async def create_web_server():
    """Create and start the aiohttp web server"""
    app = web.Application()
    app.router.add_get('/', health_check)
    app.router.add_get('/health', health_check)
    app.router.add_get('/status', bot_status)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    print(f"✅ Serveur web démarré sur 0.0.0.0:{PORT}")
    return runner

# --- LANCEMENT PRINCIPAL ---
async def main():
    """Fonction principale pour démarrer le bot"""
    print("Démarrage du bot Telegram...")

    if not API_ID or not API_HASH or not BOT_TOKEN:
        print("❌ Configuration manquante! Veuillez vérifier votre fichier .env")
        return

    try:
        # Démarrage du serveur web
        web_runner = await create_web_server()

        # Démarrage du bot
        if await start_bot():
            print("✅ Bot en ligne et en attente de messages...")
            print(f"🌐 Accès web: http://0.0.0.0:{PORT}")

            # Démarrage du surveillant de fichiers Excel en arrière-plan
            excel_watcher_task = asyncio.create_task(excel_file_watcher())

            await client.run_until_disconnected()

            # Annuler le watcher quand le bot s'arrête
            excel_watcher_task.cancel()
        else:
            print("❌ Échec du démarrage du bot")

    except KeyboardInterrupt:
        print("\n🛑 Arrêt du bot demandé par l'utilisateur")
    except Exception as e:
        print(f"❌ Erreur critique: {e}")

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Arrêt du script.")
    except Exception as e:
        print(f"Erreur fatale à l'exécution: {e}")