# Vault bot (ধাপ ১)

## চালানোর নিয়ম (Railway)
1. এই ফোল্ডার GitHub-এ আপলোড করুন, Railway-তে deploy করুন।
2. Railway Variables-এ `.env.example`-এর সব ভ্যালু বসান।
   - `WEBAPP_URL` = Railway যে লিংক দেয় (Settings > Networking > Generate Domain)
   - `DB_PATH=/data/vault.db` এবং Railway-তে একটা Volume বানিয়ে `/data` এ মাউন্ট করুন, না হলে redeploy-এ ডেটা মুছে যাবে।
3. BotFather থেকে বটের Payments ঠিক করা লাগবে না, Stars পেমেন্ট নিজে থেকেই চলে।
4. বটে `/start` দিন, তারপর `/admin` দিন।

## অ্যাডমিন প্যানেল (/admin)
ফাইল আপলোড (টাইটেল, বর্ণনা, Demo/Tutorial/Bot লিংক, ক্রেডিট ও Stars দাম), ফাইল ম্যানেজ (দাম বদলানো, লুকানো, মুছা),
ক্রেডিট দেওয়া বা কাটা, প্রোমো কোড, ইউজার দেখা ও ব্যান, ব্রডকাস্ট, স্ট্যাটস।
