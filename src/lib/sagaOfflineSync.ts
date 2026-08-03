/**
 * `src/lib/sagaOfflineSync.ts` -- Client-Side Offline PWA WAL Queue Sync.
 * 
 * IndexedDB local queue for offline transactions + Background Sync replay upon network recovery.
 * 
 * Published & Maintained by: SAGAOPS Enterprise
 * Founder & Owner: Thomas J George (thomasjgeorge23@gmail.com)
 */

export interface QueuedSagaTransaction {
  id: string;
  tool_name: string;
  payload: Record<string, any>;
  timestamp: number;
}

const DB_NAME = "AskeeSagaOfflineDB";
const STORE_NAME = "offline_sagas";

function openDB(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    if (typeof window === "undefined" || !window.indexedDB) {
      return reject(new Error("IndexedDB not supported"));
    }
    const request = window.indexedDB.open(DB_NAME, 1);
    request.onupgradeneeded = () => {
      const db = request.result;
      if (!db.objectStoreNames.contains(STORE_NAME)) {
        db.createObjectStore(STORE_NAME, { keyPath: "id" });
      }
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

export async function queueOfflineTransaction(tool_name: string, payload: Record<string, any>): Promise<QueuedSagaTransaction> {
  const db = await openDB();
  const txItem: QueuedSagaTransaction = {
    id: `tx_off_${Date.now()}_${Math.random().toString(36).substring(2, 7)}`,
    tool_name,
    payload,
    timestamp: Date.now(),
  };

  return new Promise((resolve, reject) => {
    const transaction = db.transaction(STORE_NAME, "readwrite");
    const store = transaction.objectStore(STORE_NAME);
    const req = store.add(txItem);
    req.onsuccess = () => resolve(txItem);
    req.onerror = () => reject(req.error);
  });
}

export async function getQueuedTransactions(): Promise<QueuedSagaTransaction[]> {
  try {
    const db = await openDB();
    return new Promise((resolve, reject) => {
      const transaction = db.transaction(STORE_NAME, "readonly");
      const store = transaction.objectStore(STORE_NAME);
      const req = store.getAll();
      req.onsuccess = () => resolve(req.result || []);
      req.onerror = () => reject(req.error);
    });
  } catch {
    return [];
  }
}

export async function clearQueuedTransaction(id: string): Promise<void> {
  const db = await openDB();
  return new Promise((resolve, reject) => {
    const transaction = db.transaction(STORE_NAME, "readwrite");
    const store = transaction.objectStore(STORE_NAME);
    const req = store.delete(id);
    req.onsuccess = () => resolve();
    req.onerror = () => reject(req.error);
  });
}

export async function processOfflineSagaQueue(): Promise<{ syncedCount: number; errors: number }> {
  const queue = await getQueuedTransactions();
  let syncedCount = 0;
  let errors = 0;

  for (const item of queue) {
    try {
      const res = await fetch("http://127.0.0.1:8080/api/sagas/post-listing", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(item.payload),
      });

      if (res.ok) {
        await clearQueuedTransaction(item.id);
        syncedCount++;
      } else {
        errors++;
      }
    } catch {
      errors++;
    }
  }

  return { syncedCount, errors };
}
