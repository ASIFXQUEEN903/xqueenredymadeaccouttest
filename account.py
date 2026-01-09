"""
Account Management Module for OTP Bot
Handles Pyrogram login, OTP verification, and session management
"""

import logging
import re
import threading
import time
import asyncio
from datetime import datetime
from pyrogram import Client
from pyrogram.errors import (
    PhoneNumberInvalid, PhoneCodeInvalid,
    PhoneCodeExpired, SessionPasswordNeeded, PasswordHashInvalid,
    FloodWait, PhoneCodeEmpty
)

logger = logging.getLogger(__name__)

# Global event loop for async operations
_global_event_loop = None

def get_event_loop():
    """Get or create a global event loop"""
    global _global_event_loop
    if _global_event_loop is None:
        try:
            _global_event_loop = asyncio.get_running_loop()
        except RuntimeError:
            _global_event_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(_global_event_loop)
    return _global_event_loop


# -----------------------
# ASYNC MANAGEMENT
# -----------------------
class AsyncManager:
    """Manages async operations in sync context"""
    def __init__(self):
        self.lock = threading.Lock()
        
    def run_async(self, coro):
        """Run async coroutine from sync context"""
        try:
            loop = get_event_loop()
            
            if loop.is_running():
                return self._run_in_thread(coro)
            else:
                return loop.run_until_complete(coro)
                
        except Exception as e:
            logger.error(f"Async operation failed: {e}")
            raise
    
    def _run_in_thread(self, coro):
        result = None
        exception = None
        
        def run():
            nonlocal result, exception
            try:
                new_loop = asyncio.new_event_loop()
                asyncio.set_event_loop(new_loop)
                result = new_loop.run_until_complete(coro)
                new_loop.close()
            except Exception as e:
                exception = e
        
        thread = threading.Thread(target=run)
        thread.start()
        thread.join()
        
        if exception:
            raise exception
        return result


# -----------------------
# PYROGRAM CLIENT MANAGER
# -----------------------
class PyrogramClientManager:
    def __init__(self, api_id, api_hash):
        self.api_id = api_id
        self.api_hash = api_hash
        self.lock = threading.Lock()
        
    async def create_client(self, session_string=None, name=None):
        if name is None:
            name = f"client_{int(time.time())}"
            
        client = Client(
            name=name,
            session_string=session_string,
            api_id=self.api_id,
            api_hash=self.api_hash,
            in_memory=True,
            no_updates=True,
            takeout=False,
            sleep_threshold=0
        )
        return client
    
    async def send_code(self, client, phone_number):
        try:
            if hasattr(client, 'is_connected') and client.is_connected:
                await self.safe_disconnect(client)
            
            await client.connect()
            sent_code = await client.send_code(phone_number)
            return True, sent_code.phone_code_hash, None
        except FloodWait as e:
            return False, None, f"FloodWait: Please wait {e.value} seconds"
        except Exception as e:
            return False, None, str(e)
    
    async def sign_in_with_otp(self, client, phone_number, phone_code_hash, otp_code):
        try:
            if not hasattr(client, 'is_connected') or not client.is_connected:
                await client.connect()
            
            await client.sign_in(
                phone_number=phone_number,
                phone_code=otp_code,
                phone_code_hash=phone_code_hash
            )
            return True, None, None
        except SessionPasswordNeeded:
            return False, "password_required", None
        except Exception as e:
            return False, "error", str(e)
    
    async def sign_in_with_password(self, client, password):
        try:
            if not hasattr(client, 'is_connected') or not client.is_connected:
                await client.connect()
            
            await client.check_password(password)
            return True, None
        except Exception as e:
            return False, str(e)
    
    async def get_session_string(self, client):
        try:
            if not hasattr(client, 'is_connected') or not client.is_connected:
                await client.connect()
            
            try:
                me = await client.get_me()
                if me:
                    return await client.export_session_string()
            except Exception as e:
                logger.error(e)
            return None
        except Exception as e:
            logger.error(e)
            return None
    
    async def safe_disconnect(self, client):
        try:
            if client and hasattr(client, 'is_connected') and client.is_connected:
                if hasattr(client, 'session') and client.session:
                    try:
                        await client.session.stop()
                    except:
                        pass
                await client.disconnect()
        except:
            pass


# -----------------------
# ASYNC LOGIN FLOW
# -----------------------
async def pyrogram_login_flow_async(login_states, accounts_col, user_id, phone_number, chat_id, message_id, country, api_id, api_hash):
    try:
        if user_id not in login_states:
            return False, "Session expired"
        
        manager = PyrogramClientManager(api_id, api_hash)
        client = await manager.create_client()
        
        success, phone_code_hash, error = await manager.send_code(client, phone_number)
        
        if success:
            login_states[user_id].update({
                "client": client,
                "phone": phone_number,
                "phone_code_hash": phone_code_hash,
                "step": "waiting_otp",
                "manager": manager,
                "country": country,
                "api_id": api_id,
                "api_hash": api_hash
            })
            return True, "OTP sent successfully"
        else:
            await manager.safe_disconnect(client)
            return False, error or "Failed to send OTP"
            
    except Exception as e:
        logger.error(e)
        return False, str(e)


async def verify_otp_and_save_async(login_states, accounts_col, user_id, otp_code):
    try:
        if user_id not in login_states:
            return False, "Session expired"
        
        state = login_states[user_id]
        client = state["client"]
        manager = state["manager"]
        
        success, status, error = await manager.sign_in_with_otp(
            client,
            state["phone"],
            state["phone_code_hash"],
            otp_code
        )
        
        if status == "password_required":
            login_states[user_id]["step"] = "waiting_password"
            return False, "password_required"
        
        if not success:
            await manager.safe_disconnect(client)
            login_states.pop(user_id, None)
            return False, error or "OTP verification failed"
        
        session_string = await manager.get_session_string(client)
        if not session_string:
            return False, "Failed to get session string"
        
        accounts_col.insert_one({
            "country": state["country"],
            "phone": state["phone"],
            "session_string": session_string,
            "has_2fa": False,
            "two_step_password": None,
            "status": "active",
            "used": False,
            "created_at": datetime.utcnow(),
            "created_by": user_id
        })
        
        await manager.safe_disconnect(client)
        login_states.pop(user_id, None)
        return True, "Account added successfully"
            
    except Exception as e:
        logger.error(e)
        login_states.pop(user_id, None)
        return False, str(e)


async def verify_2fa_password_async(login_states, accounts_col, user_id, password):
    try:
        state = login_states[user_id]
        client = state["client"]
        manager = state["manager"]
        
        success, error = await manager.sign_in_with_password(client, password)
        if not success:
            return False, error
        
        session_string = await manager.get_session_string(client)
        if not session_string:
            return False, "Failed to get session string"
        
        accounts_col.insert_one({
            "country": state["country"],
            "phone": state["phone"],
            "session_string": session_string,
            "has_2fa": True,
            "two_step_password": password,
            "status": "active",
            "used": False,
            "created_at": datetime.utcnow(),
            "created_by": user_id
        })
        
        await manager.safe_disconnect(client)
        login_states.pop(user_id, None)
        return True, "Account added successfully"
            
    except Exception as e:
        logger.error(e)
        login_states.pop(user_id, None)
        return False, str(e)


# -----------------------
# SYNC WRAPPER
# -----------------------
class AccountManager:
    def __init__(self, api_id=6435225, api_hash="4e984ea35f854762dcde906dce426c2d"):
        self.api_id = api_id
        self.api_hash = api_hash
        self.async_manager = AsyncManager()
        self.pyrogram_manager = PyrogramClientManager(api_id, api_hash)
    
    def pyrogram_login_flow_sync(self, login_states, accounts_col, user_id, phone_number, chat_id, message_id, country):
        return self.async_manager.run_async(
            pyrogram_login_flow_async(
                login_states, accounts_col, user_id,
                phone_number, chat_id, message_id,
                country, self.api_id, self.api_hash
            )
        )
    
    def verify_otp_and_save_sync(self, login_states, accounts_col, user_id, otp_code):
        return self.async_manager.run_async(
            verify_otp_and_save_async(login_states, accounts_col, user_id, otp_code)
        )
    
    def verify_2fa_password_sync(self, login_states, accounts_col, user_id, password):
        return self.async_manager.run_async(
            verify_2fa_password_async(login_states, accounts_col, user_id, password)
        )


__all__ = ["AsyncManager", "PyrogramClientManager", "AccountManager"]
