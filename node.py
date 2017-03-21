# must unify node and client now that connections parameters are function parameters
from itertools import groupby
from operator import itemgetter
import shutil
import math
import SocketServer
import ast
import base64
import gc
import hashlib
import os
import re
import socket
import select
import sqlite3
import sys
import threading
import time
import logging
from logging.handlers import RotatingFileHandler

from Crypto import Random
from Crypto.Hash import SHA
from Crypto.PublicKey import RSA
from Crypto.Signature import PKCS1_v1_5

log_formatter = logging.Formatter('%(asctime)s %(levelname)s %(funcName)s(%(lineno)d) %(message)s')
logFile = 'node.log'
my_handler = RotatingFileHandler(logFile, mode='a', maxBytes=5 * 1024 * 1024, backupCount=2, encoding=None, delay=0)
my_handler.setFormatter(log_formatter)
my_handler.setLevel(logging.INFO)
app_log = logging.getLogger('root')
app_log.setLevel(logging.INFO)
app_log.addHandler(my_handler)

ch = logging.StreamHandler(sys.stdout)
ch.setLevel(logging.DEBUG)
formatter = logging.Formatter('%(asctime)s %(funcName)s(%(lineno)d) %(message)s')
ch.setFormatter(formatter)
app_log.addHandler(ch)

# load config
global warning_list_limit_conf

lines = [line.rstrip('\n') for line in open('config.txt')]
for line in lines:
    if "port=" in line:
        port = line.strip('port=')
    if "genesis=" in line:
        genesis_conf = line.strip('genesis=')
    if "verify=" in line:
        verify_conf = int(line.strip('verify='))
    if "version=" in line:
        version_conf = line.strip('version=')
    if "thread_limit=" in line:
        thread_limit_conf = int(line.strip('thread_limit='))
    if "rebuild_db=" in line:
        rebuild_db_conf = int(line.strip('rebuild_db='))
    if "debug=" in line:
        debug_conf = int(line.strip('debug='))
    if "purge=" in line:
        purge_conf = int(line.strip('purge='))
    if "pause=" in line:
        pause_conf = line.strip('pause=')
    if "ledger_path=" in line:
        ledger_path_conf = line.strip('ledger_path=')
    if "hyperblocks=" in line:
        hyperblocks_conf = int(line.strip('hyperblocks='))
    if "warning_list_limit=" in line:
        warning_list_limit_conf = int(line.strip('warning_list_limit='))

app_log.info("Configuration settings loaded")
# load config
version = version_conf

def unban(ip):
    global warning_list
    global banlist
    
    warning_list = [x for x in warning_list if x != ip]
    banlist = [x for x in banlist if x != ip]

def warning(ip):
    global warning_list
    warning_list.append(ip)

def ledger_convert():
    app_log.info("Converting ledger to Hyperblocks")
    depth = 10000

    shutil.copy(ledger_path_conf, ledger_path_conf+'.hyper')
    conn = sqlite3.connect(ledger_path_conf+'.hyper')
    conn.text_factory = str
    c = conn.cursor()

    end_balance = 0
    addresses = []

    c.execute("UPDATE transactions SET address = 'Hypoblock' WHERE address = 'Hyperblock'")

    c.execute("SELECT block_height FROM transactions ORDER BY block_height DESC LIMIT 1;")
    db_block_height = c.fetchone()[0]

    for row in c.execute("SELECT * FROM transactions WHERE block_height < ? ORDER BY block_height;", (str(int(db_block_height) - depth),)):
        db_address = row[2]
        db_recipient = row[3]
        addresses.append(db_address.strip())
        addresses.append(db_recipient.strip())

    unique_addressess = set(addresses)

    for x in set(unique_addressess):
        c.execute("SELECT sum(amount) FROM transactions WHERE recipient = ? AND block_height < ?;", (x,)+(str(int(db_block_height) - depth),))
        credit = c.fetchone()[0]
        if credit == None:
            credit = 0

        c.execute("SELECT sum(amount),sum(fee),sum(reward) FROM transactions WHERE address = ? AND block_height < ?;", (x,)+(str(int(db_block_height) - depth),))
        result = c.fetchall()
        debit = result[0][0]
        fees = result[0][1]
        rewards = result[0][2]

        if debit == None:
            debit = 0
        if fees == None:
            fees = 0
        if rewards == None:
            rewards = 0

        end_balance = credit - debit - fees + rewards
        #app_log.info("Address: "+ str(x))
        #app_log.info("Balance: " + str(end_balance))

        if end_balance > 0:
            timestamp = str(time.time())
            c.execute("INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (db_block_height - depth - 1, timestamp, "Hyperblock", x, str(float(end_balance)), "0", "0", "0", "0", "0", "0", "0"))
            conn.commit()

    c.execute("DELETE FROM transactions WHERE block_height < ? AND address != 'Hyperblock';", (str(int(db_block_height) - depth),))
    conn.commit()

    c.execute("VACUUM")
    conn.close()

    os.remove(ledger_path_conf)
    os.rename(ledger_path_conf+'.hyper', ledger_path_conf)

def most_common(lst):
    return max(set(lst), key=lst.count)


def bin_convert(string):
    return ''.join(format(ord(x), 'b') for x in string)

def commit(cursor):
    # secure commit for slow nodes
    passed = 0
    while passed == 0:
        try:
            cursor.commit()
            passed = 1
        except:
            app_log.info("Retrying database execute due to " + str(e))
            pass
            # secure commit for slow nodes

def execute(cursor, what):
    # secure execute for slow nodes
    passed = 0
    while passed == 0:
        try:
            #print cursor
            #print what

            cursor.execute(what)
            passed = 1
        except Exception, e:
            app_log.info("Retrying database execute due to "+str(e))
            time.sleep(0.1)
            pass
            # secure execute for slow nodes
    return cursor

def execute_param(cursor, what, param):
    # secure execute for slow nodes
    passed = 0
    while passed == 0:
        try:
            #print cursor
            #print what
            cursor.execute(what,param)
            passed = 1
        except Exception, e:
            app_log.info("Retrying database execute due to "+str(e))
            time.sleep(0.1)
            pass
            # secure execute for slow nodes
    return cursor


def send(sdef, data):
    sdef.setblocking(0) #needs adjustments in core mechanics
    sdef.sendall(data)

def receive(sdef, slen):
    sdef.setblocking(0) #needs adjustments in core mechanics
    ready = select.select([sdef], [], [], 120)
    if ready[0]:
        data = int(sdef.recv(slen))  # receive length
        # print "To receive: "+str(data)
    else:
        raise RuntimeError("Socket timeout")


    chunks = []
    bytes_recd = 0
    while bytes_recd < data:
        ready = select.select([sdef], [], [], 240)
        if ready[0]:
            chunk = sdef.recv(min(data - bytes_recd, 2048))
            if chunk == b'':
                raise RuntimeError("Socket connection broken")
            chunks.append(chunk)
            bytes_recd = bytes_recd + len(chunk)
        else:
            raise RuntimeError("Socket timeout")
    segments = b''.join(chunks)
    # print "Received segments: "+str(segments)

    return segments

gc.enable()



global active_pool
active_pool = []
global peer_ip_list
peer_ip_list = []
global consensus_blockheight_list
consensus_blockheight_list = []
global tried
tried = []
global consensus_percentage
consensus_percentage = 100
global warning_list
warning_list = []
global banlist
banlist = []
global busy
busy = 0
global leading_node
leading_node = '127.0.0.1'


# port = 2829 now defined by config

def mempool_merge(data):
    if data == "":
        app_log.info("Mempool was empty")
    else:
        app_log.info("Mempool merging started")
        # merge mempool

        try:
            mempool = sqlite3.connect('mempool.db')
            mempool.text_factory = str
            m = mempool.cursor()

            block_list = ast.literal_eval(data)

            for transaction in block_list:  # set means unique
                mempool_timestamp = transaction[0]
                mempool_address = transaction[1][:56]
                mempool_recipient = transaction[2][:56]
                mempool_amount = '%.8f' % float(transaction[3])
                mempool_signature_enc = transaction[4]
                mempool_public_key_hashed = transaction[5]
                mempool_openfield = transaction[6]



                conn = sqlite3.connect(ledger_path_conf)
                conn.text_factory = str
                c = conn.cursor()

                ledger_in = 0
                mempool_in = 0

                acceptable = 1
                try:
                    execute_param(m,("SELECT * FROM transactions WHERE signature = ?;"),(mempool_signature_enc,))  # condition 1)
                    dummy1 = m.fetchall()[0]
                    if dummy1 != None:
                        # app_log.info("That transaction is already in our mempool")
                        acceptable = 0
                        mempool_in = 1
                except:
                    pass

                try:
                    # reject transactions which are already in the ledger
                    execute_param(c,("SELECT * FROM transactions WHERE signature = ?;"),(mempool_signature_enc,)) # condition 2
                    dummy2 = c.fetchall()[0]
                    if dummy2 != None:
                        # app_log.info("That transaction is already in our ledger")
                        # reject transactions which are already in the ledger
                        acceptable = 0
                        ledger_in = 1
                except:
                    pass

                if (mempool_in == 1) and (ledger_in == 1):  # remove from mempool if it's in both ledger and mempool already
                    try:
                        execute_param(m,("DELETE FROM transactions WHERE signature = ?;"),(mempool_signature_enc,))
                        commit(mempool)
                        app_log.info("Transaction deleted from our mempool")
                    except: #experimental try and except
                        app_log.info("Transaction was not present in the pool anymore")
                        pass #continue to mempool finished message
                if acceptable == 1:
                    # verify signatures and balances
                    # verify balance
                    conn = sqlite3.connect(ledger_path_conf)
                    conn.text_factory = str
                    c = conn.cursor()

                    # app_log.info("Mempool: Verifying balance")
                    app_log.info("Mempool: Received address: " + str(mempool_address))

                    # include the new block
                    block_credit = 0
                    credit_block = 0

                    for x in block_list:  # quite nasty, care not to overlap variables
                        if x[2] == mempool_address:
                            block_credit = float(block_credit) + float(x[3])
                        if x[1] == mempool_address:
                            credit_block = float(credit_block) + float(x[3])

                    # app_log.info("Mempool: Incoming block credit: " + str(block_credit))
                    # app_log.info("Mempool: Incoming block debit: " + str(credit_block))
                    # include the new block

                    execute_param(c,("SELECT sum(amount) FROM transactions WHERE recipient = ?;"),(mempool_address,))
                    credit_ledger = c.fetchone()[0]
                    if credit_ledger == None:
                        credit_ledger = 0
                    credit = float(credit_ledger) + float(block_credit)

                    execute_param(c,("SELECT sum(amount) FROM transactions WHERE address = ?;"),(mempool_address,))
                    debit_ledger = c.fetchone()[0]
                    if debit_ledger == None:
                        debit_ledger = 0
                    debit = float(debit_ledger) + float(credit_block)

                    execute_param(c,("SELECT sum(fee) FROM transactions WHERE address = ?;"),(mempool_address,))
                    fees = c.fetchone()[0]
                    execute_param(c,("SELECT sum(reward) FROM transactions WHERE address = ?;"),(mempool_address,))
                    rewards = c.fetchone()[0]

                    if fees == None:
                        fees = 0
                    if rewards == None:
                        rewards = 0

                    # app_log.info("Mempool: Total credit: " + str(credit))
                    # app_log.info("Mempool: Total debit: " + str(debit))
                    balance = float(credit) - float(debit) - float(fees) + float(rewards)
                    # app_log.info("Mempool: Projected transction address balance: " + str(balance))



                    try:
                        execute(c, ("SELECT block_height,timestamp FROM transactions WHERE reward != 0 ORDER BY block_height DESC LIMIT 1;"))
                        result = c.fetchall()
                        db_block_height = result[0][0]
                        db_timestamp_last = float(result[0][1])

                        execute_param(c, ("SELECT avg(timestamp) FROM transactions where block_height >= ? and reward != 0;"),(str(db_block_height - 30),))
                        timestamp_avg = c.fetchall()[0][0]  # select the reward block

                        conn.close()

                        fee = abs(1000 / (float(db_timestamp_last) - float(timestamp_avg))) + len(mempool_openfield) / 600
                        # app_log.info("Fee: " + str(fee))

                    except Exception as e:
                        fee = 1  # presumably there are less than 50 txs
                        # app_log.info("Mempool: Fee error: " + str(e))
                        return
                    # calculate fee

                    time_now = str(time.time())
                    if float(time_now) + 30 < float(mempool_timestamp):
                        app_log.info("Mempool: Future mining not allowed")

                    elif (float(balance)) - (
                            float(fee)) < 0:  # removed +float(db_amount) because it is a part of the incoming block
                        app_log.info("Mempool: Cannot afford to pay fees")
                    # verify signatures and balances
                    else:
                        execute_param(m,"INSERT INTO transactions VALUES (?,?,?,?,?,?,?)", (
                            mempool_timestamp, mempool_address, mempool_recipient, str(float(mempool_amount)),
                            mempool_signature_enc, mempool_public_key_hashed, mempool_openfield))
                        app_log.info("Mempool updated with a received transaction")
                        commit(mempool)  # Save (commit) the changes

                            # merge mempool

                            # receive mempool

            app_log.info("Mempool: Finished")
            mempool.close()
        except:
            app_log.info("Mempool: Error processing")  # will this fix the hang?
            if debug_conf == 1:
                raise
            else:
                return


def purge_old_peers():
    with open("peers.txt", "r") as peer_list:
        peers = peer_list.read()
        peer_tuples = re.findall("'([\d\.]+)', '([\d]+)'", peers)
        # app_log.info(peer_tuples)

        for tuple in peer_tuples:
            HOST = tuple[0]
            # app_log.info(HOST)
            PORT = int(tuple[1])
            # app_log.info(PORT)

            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                #s.setblocking(0)
                s.connect((HOST, PORT))
                s.close()
            except:
                if purge_conf == 1:
                    # remove from peerlist if not connectible
                    peer_tuples.remove((HOST, str(PORT)))
                    app_log.info("Removed formerly active peer " + str(HOST) + " " + str(PORT))
                pass

            output = open("peers.txt", 'w')
            for x in peer_tuples:
                output.write(str(x) + "\n")
            output.close()


def verify():
    try:
        # verify blockchain
        conn = sqlite3.connect(ledger_path_conf)
        conn.text_factory = str
        c = conn.cursor()
        # c.execute("CREATE TABLE IF NOT EXISTS transactions (block_height, address, recipient, amount, signature, public_key)")
        execute(c,("SELECT Count(*) FROM transactions"))
        db_rows = c.fetchone()[0]
        app_log.info("Total steps: " + str(db_rows))

        # verify genesis
        execute(c,("SELECT recipient FROM transactions ORDER BY block_height ASC LIMIT 1"))
        genesis = c.fetchone()[0]
        app_log.info("Genesis: " + genesis)
        if str(
                genesis) != genesis_conf:  # change this line to your genesis address if you want to clone
            app_log.info("Invalid genesis address")
            sys.exit(1)
        # verify genesis

        invalid = 0
        for row in execute(c,('SELECT * FROM transactions ORDER BY block_height')):
            db_block_height = row[0]
            db_timestamp = row[1]
            db_address = row[2]
            db_recipient = row[3]
            db_amount = row[4]
            db_signature_enc = row[5]
            db_public_key_hashed = row[6]
            db_public_key = RSA.importKey(base64.b64decode(db_public_key_hashed))
            db_openfield = row[11]

            db_transaction = (db_timestamp, db_address, db_recipient, str(float(db_amount)), db_openfield)

            db_signature_dec = base64.b64decode(db_signature_enc)
            verifier = PKCS1_v1_5.new(db_public_key)
            h = SHA.new(str(db_transaction))
            if verifier.verify(h, db_signature_dec):
                pass
            else:
                app_log.info("The following transaction is invalid:")
                app_log.info(row)
                invalid = invalid + 1
                if db_block_height == str(1):
                    app_log.info("Your genesis signature is invalid, someone meddled with the database")
                    sys.exit(1)

        if invalid == 0:
            app_log.info("All transacitons in the local ledger are valid")

    except sqlite3.Error, e:
        app_log.info("Error %s:" % e.args[0])
        sys.exit(1)
    finally:
        if conn:
            conn.close()
            # verify blockchain


def blocknf(block_hash_delete):
    global busy
    if busy == 0:
        busy = 1
        try:
            conn = sqlite3.connect(ledger_path_conf)
            conn.text_factory = str
            c = conn.cursor()

            execute(c,('SELECT * FROM transactions ORDER BY block_height DESC LIMIT 1'))
            results = c.fetchone()
            db_block_height = results[0]
            db_timestamp = results[1]
            # db_address = results[2]
            # db_recipient = results[3]
            # db_amount = results[4]
            # db_signature = results[5]
            # db_public_key_hashed = results[6]
            db_block_hash = results[7]
            db_confirmations = results[10]

            if db_block_height < 2:
                app_log.info("Outgoing: Will not roll back this block")
                conn.close()

            elif (db_block_hash != block_hash_delete):
                # print db_block_hash
                # print block_hash_delete
                app_log.info("Outgoing: We moved away from the block to rollback, skipping")
                conn.close()

            else:
                # delete followups
                execute_param(c,("DELETE FROM transactions WHERE block_height >= ?;"),(str(db_block_height),))
                commit(conn)
                conn.close()

                app_log.info("Outgoing: Node didn't find the block, deleted latest entry")  # PRONE TO ATTACK



        except:
            pass
        busy = 0

        # delete followups


def consensus_add(peer_ip, consensus_blockheight):
    global leading_node
    global peer_ip_list
    global consensus_blockheight_list
    global consensus_percentage

    if peer_ip not in peer_ip_list:
        app_log.info("Adding " + str(peer_ip) + " to consensus peer list")
        peer_ip_list.append(peer_ip)
        app_log.info("Assigning " + str(consensus_blockheight) + " to peer block height list")
        consensus_blockheight_list.append(str(int(consensus_blockheight)))

    if peer_ip in peer_ip_list:
        consensus_index = peer_ip_list.index(peer_ip)  # get where in this list it is

        if consensus_blockheight_list[consensus_index] == (consensus_blockheight):
            app_log.info("Opinion of " + str(peer_ip) + " hasn't changed")

        else:
            del peer_ip_list[consensus_index]  # remove ip
            del consensus_blockheight_list[consensus_index]  # remove ip's opinion

            app_log.info("Updating " + str(peer_ip) + " in consensus")
            peer_ip_list.append(peer_ip)
            consensus_blockheight_list.append(int(consensus_blockheight))

    app_log.info("Consensus IP list:" + str(peer_ip_list))
    app_log.info("Consensus opinion list:" + str(consensus_blockheight_list))

    consensus = most_common(consensus_blockheight_list)

    consensus_percentage = (float(
        consensus_blockheight_list.count(consensus) / float(len(consensus_blockheight_list)))) * 100
    app_log.info("Current outgoing connections: " + str(len(active_pool)))
    app_log.info("Current block consensus: " + str(consensus) + " = " + str(consensus_percentage) + "%")

    if max(consensus_blockheight_list) == consensus_blockheight:
        leading_node = peer_ip
        app_log.info("Leading node is now "+str(leading_node))

    return


def consensus_remove(peer_ip):
    global peer_ip_list
    global consensus_blockheight_list
    if peer_ip in peer_ip_list:
        app_log.info(
            "Will remove " + str(peer_ip) + " from consensus pool " + str(peer_ip_list))
        consensus_index = peer_ip_list.index(peer_ip)
        peer_ip_list.remove(peer_ip)
        del consensus_blockheight_list[consensus_index]  # remove ip's opinion
    else:
        app_log.info("IP of " + str(peer_ip) + " not present in the consensus pool")


def manager():
    global banlist
    while True:
        with open("peers.txt", "r") as peer_list:
            peers = peer_list.read()
            peer_tuples = re.findall("'([\d\.]+)', '([\d]+)'", peers)
            # app_log.info(peer_tuples)

            threads_count = threading.active_count()
            threads_limit = thread_limit_conf

            for tuple in peer_tuples:
                HOST = tuple[0]
                # app_log.info(HOST)
                PORT = int(tuple[1])
                # app_log.info(PORT)

                if threads_count <= threads_limit and str(HOST + ":" + str(PORT)) not in tried and str(
                                        HOST + ":" + str(PORT)) not in active_pool and str(HOST) not in banlist:
                    app_log.info("Will attempt to connect to " + HOST + ":" + str(PORT))
                    tried.append(HOST + ":" + str(PORT))
                    t = threading.Thread(target=worker, args=(HOST, PORT))  # threaded connectivity to nodes here
                    app_log.info("---Starting a client thread " + str(threading.currentThread()) + "---")
                    t.start()

                    # client thread handling
        if len(active_pool) < 3:
            app_log.info("Only " + str(len(active_pool)) + " connections active, resetting the try list")
            del tried[:]

        app_log.info("Connection manager: Threads at " + str(threads_count) + "/" + str(threads_limit))
        app_log.info("Tried: " + str(tried))
        app_log.info("Current active pool: " + str(active_pool))
        app_log.info("Current connections: " + str(len(active_pool)))

        # app_log.info(threading.enumerate() all threads)
        time.sleep(int(pause_conf))


def digest_block(data,peer_ip):
    global warning_list
    global busy

    if busy == 0:
        busy = 1

        try:
            conn = sqlite3.connect(ledger_path_conf)
            conn.text_factory = str
            c = conn.cursor()

            mempool = sqlite3.connect('mempool.db')
            mempool.text_factory = str
            m = mempool.cursor()

            # remove possible duplicates

            execute(c,("select block_height, count(*) FROM transactions WHERE signature != '0' GROUP by signature HAVING count(*) > 1"))
            result = c.fetchall()
            for x in result:
                #print x
                app_log.info("Removing duplicate: " + str(x[0]))
                execute_param(c,("DELETE FROM transactions WHERE block_height >= ?;"),(str(x[0]),))
                commit(conn)

            if result:
                raise ValueError("Skipping new block because duplicates were removed")
            # remove possible duplicates

            block_valid = 1 #init

            # app_log.info("Incoming: Digesting incoming block: " + data)

            block_list = ast.literal_eval(data)
            if not any(isinstance(el, list) for el in block_list): #if it's not a list of lists
                new_list = []
                new_list.append(block_list)
                block_list = new_list #make it a list of lists
            #print block_list

            # reject block with duplicate transactions
            signature_list = []
            block_transactions = []

            for transaction_list in block_list:

                for r in transaction_list:  # sig 4
                    signature_list.append(r[4])

                    # reject block with transactions which are already in the ledger
                    execute_param(c,("SELECT block_height FROM transactions WHERE signature = ?;"),(r[4],))
                    try:
                        result = c.fetchall()[0]
                        app_log.info("That transaction is already in our ledger, row "+str(result[0]))
                        block_valid = 0

                    except:
                        pass
                        # reject block with transactions which are already in the ledger

                if len(signature_list) != len(set(signature_list)):
                    app_log.info("There are duplicate transactions in this block, rejected")
                    block_valid = 0  # dont really need this one
                del signature_list[:]

                # reject block with duplicate transactions

                for transaction in transaction_list:
                    # verify signatures
                    received_timestamp = transaction[0]
                    received_address = transaction[1][:56]
                    received_recipient = transaction[2][:56]
                    received_amount = '%.8f' % float(transaction[3])
                    received_signature_enc = transaction[4]
                    received_public_key_hashed = transaction[5]
                    received_openfield = transaction[6]

                    received_public_key = RSA.importKey(
                        base64.b64decode(received_public_key_hashed))  # convert readable key to instance
                    received_signature_dec = base64.b64decode(received_signature_enc)
                    verifier = PKCS1_v1_5.new(received_public_key)

                    h = SHA.new(str((received_timestamp, received_address, received_recipient, received_amount,
                                     received_openfield)))
                    if verifier.verify(h, received_signature_dec):
                        app_log.info("Incoming: The signature is valid")

                    if transaction == transaction_list[-1]:  # recognize the last transaction as the mining reward transaction
                        miner_address = received_address
                        block_timestamp = received_timestamp


                        # verify signatures

                execute(c,("SELECT block_hash, block_height,timestamp FROM transactions WHERE reward != 0 ORDER BY block_height DESC LIMIT 1;"))
                result = c.fetchall()
                db_block_height = result[0][1]
                db_block_hash = result[0][0]
                db_timestamp_last = float(result[0][2])
                block_height_new = db_block_height + 1

                # reject blocks older than latest block
                if block_timestamp < db_timestamp_last:
                    block_valid = 0
                    app_log.info("Block is older than the previous one, will be rejected")
                # reject blocks older than latest block

                # calculate difficulty
                execute_param(c,("SELECT avg(timestamp) FROM transactions where block_height >= ? and reward != 0;"),(str(db_block_height - 30),))
                timestamp_avg = c.fetchall()[0][0]  # select the reward block
                # print timestamp_avg

                timestamp_difference = db_timestamp_last - timestamp_avg
                # print timestamp_difference

                diff = int(math.log(1e18 / timestamp_difference))
                if db_block_height < 50:
                    diff = 33
                # if diff < 4:
                #    diff = 4

                app_log.info("Calculated difficulty: " + str(diff))
                # calculate difficulty

                # match difficulty
                block_hash = hashlib.sha224(str((block_timestamp, transaction_list, db_block_hash))).hexdigest()  # calculate block_hash from the ledger

                if bin_convert(miner_address)[0:diff] in bin_convert(block_hash):  # simplified comparison, no backwards mining
                    app_log.info("Digest: Difficulty requirement satisfied")
                else:
                    # app_log.info("Digest: Difficulty requirement not satisfied: " + bin_convert(miner_address) + " " + bin_convert(block_hash))
                    app_log.info("Digest: Difficulty requirement not satisfied")
                    block_valid = 0
                #match difficulty

                fees_block = []

                if block_valid == 1:
                    for transaction in transaction_list:
                        db_timestamp = transaction[0]
                        db_address = transaction[1][:56]
                        db_recipient = transaction[2][:56]
                        db_amount = '%.8f' % float(transaction[3])
                        db_signature = transaction[4]
                        db_public_key_hashed = transaction[5]
                        db_openfield = transaction[6]

                        # print "sync this"
                        # print block_timestamp
                        # print transaction_list
                        # print db_block_hash
                        # print (str((block_timestamp,transaction_list,db_block_hash)))

                        # app_log.info("Digest: tx sig not found in the local ledger, proceeding to check before insert")

                        # app_log.info("Digest: Verifying balance")
                        # app_log.info("Digest: Received address: " + str(db_address))

                        # include the new block
                        block_credit = 0
                        credit_block = 0

                        for x in transaction_list:  # quite nasty, care not to overlap variables
                            if x[2] == db_address:
                                block_credit = float(block_credit) + float(x[3])
                            if x[1] == db_address:
                                credit_block = float(credit_block) + float(x[3])

                        # app_log.info("Digest: Incoming block credit: " + str(block_credit))
                        # app_log.info("Digest: Incoming block debit: " + str(credit_block))
                        # include the new block

                        execute_param(c,("SELECT sum(amount) FROM transactions WHERE recipient = ?;"),(db_address,))
                        credit_ledger = c.fetchone()[0]
                        if credit_ledger == None:
                            credit_ledger = 0
                        credit = float(credit_ledger) + float(block_credit)

                        execute_param(c,("SELECT sum(amount) FROM transactions WHERE address = ?;"),(db_address,))
                        debit_ledger = c.fetchone()[0]
                        if debit_ledger == None:
                            debit_ledger = 0
                        debit = float(debit_ledger) + float(credit_block)

                        execute_param(c,("SELECT sum(fee),sum(reward) FROM transactions WHERE address = ?;"),(db_address,))
                        result = c.fetchall()[0]
                        fees = result[0]
                        rewards = result[1]

                        if fees == None:
                            fees = 0
                        if rewards == None:
                            rewards = 0

                        # app_log.info("Digest: Total credit: " + str(credit))
                        # app_log.info("Digest: Total debit: " + str(debit))
                        balance = float(credit) - float(debit) - float(fees) + float(rewards)
                        # app_log.info("Digest: Projected transction address balance: " + str(balance))

                        db_block_50 = int(db_block_height) - 50
                        try:
                            execute_param(c,("SELECT timestamp FROM transactions WHERE block_height = ?;"),(str(db_block_50),))
                            db_timestamp_50 = c.fetchone()[0]
                            fee = abs(1000 / (float(db_timestamp) - float(db_timestamp_50))) + len(db_openfield) / 600
                            fees_block.append(fee)
                            # app_log.info("Fee: " + str(fee))

                        except Exception as e:
                            fee = 1  # presumably there are less than 50 txs
                            # app_log.info("Fee error: " + str(e))
                            # return #debug
                        # calculate fee

                        # decide reward

                        time_now = str(time.time())
                        if float(time_now) + 30 < float(db_timestamp):
                            app_log.info("Digest: Future mining not allowed")
                            block_valid = 0

                        else:
                            if transaction == transaction_list[-1]:

                                if db_block_height <= 10000000:
                                    mining_reward = 15 - (float(block_height_new) / float(1000000)) #one zero less
                                else:
                                    mining_reward = 0

                                reward = mining_reward + sum(fees_block[:-1])
                                fee = 0
                            else:
                                reward = 0

                                # dont request a fee for mined block so new accounts can mine

                            if (float(balance)) - (float(fee)) < 0:  # removed +float(db_amount) because it is a part of the incoming block
                                app_log.info("Digest: Cannot afford to pay fees")
                                block_valid = 0

                            else:
                                # append, but do not insert to ledger before whole block is validated
                                app_log.info("Digest: Appending transaction back to block with "+str(len(block_transactions))+" transactions in it")
                                block_transactions.append((block_height_new, db_timestamp, db_address, db_recipient,
                                                           str(float(db_amount)), db_signature, db_public_key_hashed,
                                                           block_hash, fee, reward, str(0), db_openfield))


                        try:
                            execute_param(m,("DELETE FROM transactions WHERE signature = ?;"),(db_signature,))  # delete tx from mempool now that it is in the ledger
                            commit(mempool)
                            app_log.info("Digest: Removed processed transaction from the mempool")
                        except:
                            # tx was not in the local mempool
                            pass

                    # whole block validation
                    for transaction in block_transactions:
                        #print transaction
                        execute_param(c,"INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (
                            transaction[0], transaction[1][:56], transaction[2][:56], transaction[3], transaction[4], transaction[5],
                            transaction[6], transaction[7], transaction[8], transaction[9], transaction[10],
                            transaction[11]))
                        #secure commit for slow nodes
                        commit(conn)
                    app_log.info("Block " + transaction[0] + " valid and saved")
                    del block_transactions[:]
                    unban(peer_ip)
                    
                else:
                    app_log.info("A part of the block is invalid, rejected")
                    warning(peer_ip)

                    # whole block validation
        except Exception, e:
            app_log.info(e)
            pass

        conn.close()
        mempool.close()
        app_log.info("Digesting complete")
        busy = 0
        return


def db_maintenance():
    # db maintenance
    conn = sqlite3.connect(ledger_path_conf)
    execute(conn, "VACUUM")
    conn.close()
    conn = sqlite3.connect("mempool.db")
    execute(conn, "VACUUM")
    conn.close()
    app_log.info("Database maintenance finished")


# key maintenance
if os.path.isfile("privkey.der") is True:
    app_log.info("privkey.der found")
elif os.path.isfile("privkey_encrypted.der") is True:
    app_log.info("privkey_encrypted.der found")
else:
    # generate key pair and an address
    random_generator = Random.new().read
    key = RSA.generate(1024, random_generator)
    public_key = key.publickey()

    private_key_readable = str(key.exportKey())
    public_key_hashed = str(key.publickey().exportKey())
    address = hashlib.sha224(public_key_hashed).hexdigest()  # hashed public key
    # generate key pair and an address

    app_log.info("Your address: " + str(address))
    app_log.info("Your private key: " + str(private_key_readable))
    app_log.info("Your public key: " + str(public_key_hashed))

    pem_file = open("privkey.der", 'a')
    pem_file.write(str(private_key_readable))
    pem_file.close()

    pem_file = open("pubkey.der", 'a')
    pem_file.write(str(public_key_hashed))
    pem_file.close()

    address_file = open("address.txt", 'a')
    address_file.write(str(address) + "\n")
    address_file.close()

# import keys
#key = RSA.importKey(open('privkey.der').read())
#private_key_readable = str(key.exportKey())
public_key_readable = open('pubkey.der').read()
public_key_hashed = base64.b64encode(public_key_readable)
address = hashlib.sha224(public_key_readable).hexdigest()


app_log.info("Local address: " + str(address))

if hyperblocks_conf == 1:
    ledger_convert()

if not os.path.exists('mempool.db'):
    # create empty mempool
    mempool = sqlite3.connect('mempool.db')
    mempool.text_factory = str
    m = mempool.cursor()
    execute(m,("CREATE TABLE IF NOT EXISTS transactions (timestamp, address, recipient, amount, signature, public_key, openfield)"))
    commit(mempool)
    mempool.close()
    app_log.info("Created mempool file")
    # create empty mempool
else:
    app_log.info("Mempool exists")

if rebuild_db_conf == 1:
    db_maintenance()
# connectivity to self node

if verify_conf == 1:
    verify()


### LOCAL CHECKS FINISHED ###
app_log.info("Starting up...")
class ThreadedTCPRequestHandler(SocketServer.BaseRequestHandler):
    def handle(self):  # server defined here
        global leading_node
        global busy
        global banlist
        global warning_list_limit_conf

        peer_ip = self.request.getpeername()[0]

        if threading.active_count() < thread_limit_conf:
            capacity = 1
        else:
            capacity = 0
            self.request.close()
            app_log.info("Free capacity for " + peer_ip + " unavailable, disconnected")

            if debug_conf == 1:
                raise  # major debug client
            else:
                return

        if peer_ip not in banlist:
            banned = 0
        else:
            banned = 1
            self.request.close()
            app_log.info("IP " + peer_ip + " banned, disconnected")

            if debug_conf == 1:
                raise  # major debug client
            else:
                return

        if warning_list.count(peer_ip) >= warning_list_limit_conf:
            banlist.append(peer_ip)

        while banned == 0 and capacity == 1:

            try:
                data = receive(self.request, 10)

                app_log.info("Incoming: Received: " + str(data) + " from " + str(peer_ip))  # will add custom ports later

                if data == 'version':
                    data = receive(self.request, 10)
                    if version != data:
                        app_log.info("Protocol version mismatch: " + data + ", should be " + version)
                        send(self.request, (str(len("notok"))).zfill(10))
                        send(self.request, "notok")
                        return
                    else:
                        app_log.info("Incoming: Protocol version matched: " + data)
                        send(self.request, (str(len("ok"))).zfill(10))
                        send(self.request, "ok")

                elif data == 'mempool':

                    # receive theirs
                    segments = receive(self.request, 10)
                    mempool_merge(segments)
                    # receive theirs

                    mempool = sqlite3.connect('mempool.db')
                    mempool.text_factory = str
                    m = mempool.cursor()
                    execute(m,('SELECT * FROM transactions'))
                    mempool_txs = m.fetchall()

                    # send own
                    #app_log.info("Incoming: Extracted from the mempool: " + str(mempool_txs))  # improve: sync based on signatures only

                    #if len(mempool_txs) > 0: same as the other
                    send(self.request, (str(len(str(mempool_txs)))).zfill(10))
                    send(self.request, str(mempool_txs))
                    # send own

                elif data == 'hello':
                    with open("peers.txt", "r") as peer_list:
                        peers = peer_list.read()

                        send(self.request, (str(len("peers"))).zfill(10))
                        send(self.request, "peers")

                        send(self.request, (str(len(peers))).zfill(10))
                        send(self.request, str(peers))

                    peer_list.close()

                    # save peer if connectible
                    peer_file = open("peers.txt", 'r')
                    peer_tuples = []
                    for line in peer_file:
                        extension = re.findall("'([\d\.]+)', '([\d]+)'", line)
                        peer_tuples.extend(extension)
                    peer_file.close()
                    peer_tuple = ("('" + peer_ip + "', '" + str(port) + "')")

                    try:
                        app_log.info("Testing connectivity to: " + str(peer_ip))
                        peer_test = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                        #peer_test.setblocking(0)
                        peer_test.connect((str(peer_ip), int(str(port))))  # double parentheses mean tuple
                        app_log.info("Incoming: Distant peer connectible")

                        # properly end the connection
                        peer_test.close()
                        # properly end the connection
                        if peer_tuple not in str(peer_tuples):  # stringing tuple is a nasty way
                            peer_list_file = open("peers.txt", 'a')
                            peer_list_file.write((peer_tuple) + "\n")
                            app_log.info("Incoming: Distant peer saved to peer list")
                            peer_list_file.close()
                        else:
                            app_log.info("Distant peer already in peer list")
                    except:
                        app_log.info("Incoming: Distant peer not connectible")
                        pass

                        # raise #test only

                    # save peer if connectible

                    while busy == 1:
                        time.sleep(1)
                    app_log.info("Incoming: Sending sync request")
                    send(self.request, (str(len("sync"))).zfill(10))
                    send(self.request, "sync")

                elif data == "sendsync":
                    while busy == 1:
                        time.sleep(1)

                    send(self.request, (str(len("sync"))).zfill(10))
                    send(self.request, "sync")

                elif data == "blocksfnd":
                    app_log.info("Incoming: Client has the block")  # node should start sending txs in this step

                    # receive theirs
                    segments = receive(self.request, 10)

                    # app_log.info("Incoming: Combined segments: " + segments)
                    #print peer_ip
                    #print leading_node
                    if peer_ip == leading_node:
                        digest_block(segments, peer_ip)
                        # receive theirs

                    while busy == 1:
                        time.sleep(1)
                    send(self.request, (str(len("sync"))).zfill(10))
                    send(self.request, "sync")

                elif data == "blockheight":
                    received_block_height = receive(self.request, 10)  # receive client's last block height
                    app_log.info("Incoming: Received block height: " + received_block_height)

                    # consensus pool 1 (connection from them)
                    consensus_blockheight = int(received_block_height)  # str int to remove leading zeros
                    consensus_add(peer_ip, consensus_blockheight)
                    # consensus pool 1 (connection from them)

                    conn = sqlite3.connect(ledger_path_conf)
                    conn.text_factory = str
                    c = conn.cursor()
                    execute(c,('SELECT block_height FROM transactions ORDER BY block_height DESC LIMIT 1'))
                    db_block_height = c.fetchone()[0]
                    conn.close()

                    # append zeroes to get static length
                    send(self.request, (str(len(str(db_block_height)))).zfill(10))
                    send(self.request, str(db_block_height))
                    # send own block height

                    if int(received_block_height) > db_block_height:
                        app_log.info("Incoming: Client has higher block")
                        update_me = 1

                    if int(received_block_height) < db_block_height:
                        app_log.info("Incoming: We have a higher block, hash will be verified")
                        update_me = 0

                    if int(received_block_height) == db_block_height:
                        app_log.info("Incoming: We have the same block height, hash will be verified")
                        update_me = 0

                    # print "Update me:" + str(update_me)
                    if update_me == 1:
                        conn = sqlite3.connect(ledger_path_conf)
                        conn.text_factory = str
                        c = conn.cursor()
                        execute(c,('SELECT block_hash FROM transactions ORDER BY block_height DESC LIMIT 1'))
                        db_block_hash = c.fetchone()[0]  # get latest block_hash
                        conn.close()

                        app_log.info("Incoming: block_hash to send: " + str(db_block_hash))
                        send(self.request, (str(len(db_block_hash))).zfill(10))
                        send(self.request, str(db_block_hash))

                        # receive their latest hash
                        # confirm you know that hash or continue receiving

                    if update_me == 0:  # update them if update_me is 0
                        data = receive(self.request, 10)  # receive client's last block_hash
                        # send all our followup hashes

                        app_log.info("Incoming: Will seek the following block: " + str(data))

                        conn = sqlite3.connect(ledger_path_conf)
                        conn.text_factory = str
                        c = conn.cursor()

                        try:
                            execute_param(c,("SELECT block_height FROM transactions WHERE block_hash = ?;"),(data,))
                            client_block = c.fetchone()[0]

                            app_log.info("Incoming: Client is at block " + str(
                                client_block))  # now check if we have any newer

                            execute(c,('SELECT block_hash FROM transactions ORDER BY block_height DESC LIMIT 1'))
                            db_block_hash = c.fetchone()[0]  # get latest block_hash
                            if db_block_hash == data:
                                app_log.info("Incoming: Client has the latest block")
                                send(self.request, (str(len("nonewblk"))).zfill(10))
                                send(self.request, "nonewblk")

                            else:
                                execute_param(c,("SELECT block_height, timestamp,address,recipient,amount,signature,public_key,openfield FROM transactions WHERE block_height > ? AND block_height < ?;"),(str(int(client_block)),)+(str(int(client_block + 100)),))  # select incoming transaction + 1, only columns that need not be verified
                                blocks_fetched = c.fetchall()
                                blocks_send = [[l[1:] for l in group] for _, group in groupby(blocks_fetched, key=itemgetter(0))]

                                #app_log.info("Incoming: Selected " + str(blocks_send) + " to send")

                                conn.close()
                                send(self.request, (str(len("blocksfnd"))).zfill(10))
                                send(self.request, "blocksfnd")

                                send(self.request, (str(len(str(blocks_send)))).zfill(10))
                                send(self.request, str(blocks_send))
                                # send own

                        except:
                            app_log.info("Incoming: Block not found")
                            send(self.request, (str(len("blocknf"))).zfill(10))
                            send(self.request, "blocknf")

                            send(self.request, (str(len(data))).zfill(10))
                            send(self.request, data)

                elif data == "nonewblk":
                    # digest_block() #temporary #otherwise passive node will not be able to digest

                    send(self.request, (str(len("sync"))).zfill(10))
                    send(self.request, "sync")

                elif data == "blocknf":
                    block_hash_delete = receive(self.request, 10)
                    #print peer_ip
                    #print leading_node
                    if peer_ip == leading_node:
                        blocknf(block_hash_delete)
                        warning_list.append(peer_ip)

                    while busy == 1:
                        time.sleep(1)
                    app_log.info("Outgoing: Deletion complete, sending sync request")

                    send(self.request, (str(len("sync"))).zfill(10))
                    send(self.request, "sync")

                elif data == "block":  # from miner
                    # receive theirs
                    segments = receive(self.request, 10)
                    # app_log.info("Incoming: Combined mined segments: " + segments)
                    digest_block(segments, peer_ip)
                    # receive theirs

                else:
                    raise ValueError("Unexpected error, received: " + str(data))

                time.sleep(0.1)  # prevent cpu overload
                # app_log.info("Server resting")

            except Exception, e:
                app_log.info("Incoming: Lost connection to " + str(peer_ip))
                app_log.info("Incoming: " + str(e))

                # remove from consensus (connection from them)
                consensus_remove(peer_ip)
                # remove from consensus (connection from them)
                if self.request:
                    self.request.close()
                if debug_conf == 1:
                    raise  # major debug client
                else:
                    return


# client thread
def worker(HOST, PORT):
    global leading_node
    global busy
    try:
        this_client = (HOST + ":" + str(PORT))
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        #s.setblocking(0)
        s.connect((HOST, PORT))
        app_log.info("Outgoing: Connected to " + this_client)

        if this_client not in active_pool:
            active_pool.append(this_client)
            app_log.info("Current active pool: " + str(active_pool))

    except:
        app_log.info("Could not connect to " + this_client)
        return

    first_run = 1

    while True:
        peer_ip = s.getpeername()[0]
        try:
            # communication starter
            if first_run == 1:
                first_run = 0

                send(s, (str(len("version"))).zfill(10))
                send(s, "version")

                send(s, (str(len(version))).zfill(10))
                send(s, version)

                data = receive(s, 10)

                if (data == "ok"):
                    app_log.info("Outgoing: Node protocol version matches our client")
                else:
                    app_log.info("Outgoing: Node protocol version mismatch")
                    return

                send(s, (str(len("hello"))).zfill(10))
                send(s, "hello")

            # communication starter

            data = receive(s, 10)  # receive data, one and the only root point

            # if data:
            #    timer = time.time() #reset timer

            if data == "peers":
                subdata = receive(s, 10)

                # get remote peers into tuples
                server_peer_tuples = re.findall("'([\d\.]+)', '([\d]+)'", subdata)
                app_log.info("Received following " + str(len((server_peer_tuples))) + " peers: " + str(server_peer_tuples))
                # get remote peers into tuples

                # get local peers into tuples
                peer_file = open("peers.txt", 'r')
                peer_tuples = []
                for line in peer_file:
                    extension = re.findall("'([\d\.]+)', '([\d]+)'", line)
                    peer_tuples.extend(extension)
                peer_file.close()
                # get local peers into tuples

                for x in server_peer_tuples:
                    if x not in peer_tuples:
                        app_log.info("Outgoing: " + str(x) + " is a new peer, saving if connectible")
                        try:
                            s_purge = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                            #s_purge = s.setblocking(0)
                            s_purge.connect((HOST[x], PORT[x]))  # save a new peer file with only active nodes

                            s_purge.close()

                            peer_list_file = open("peers.txt", 'a')
                            peer_list_file.write(str(x) + "\n")
                            peer_list_file.close()
                        except:
                            app_log.info("Not connectible")

                    else:
                        app_log.info("Outgoing: " + str(x) + " is not a new peer")

            elif data == "sync":
                # sync start

                # send block height, receive block height
                send(s, (str(len("blockheight"))).zfill(10))
                send(s, "blockheight")

                conn = sqlite3.connect(ledger_path_conf)
                conn.text_factory = str
                c = conn.cursor()
                execute(c,('SELECT block_height FROM transactions ORDER BY block_height DESC LIMIT 1'))
                db_block_height = c.fetchone()[0]
                conn.close()

                app_log.info("Outgoing: Sending block height to compare: " + str(db_block_height))
                # append zeroes to get static length
                send(s, (str(len(str(db_block_height)))).zfill(10))
                send(s, str(db_block_height))

                received_block_height = receive(s, 10)  # receive node's block height
                app_log.info("Outgoing: Node is at block height: " + str(received_block_height))

                if int(received_block_height) < db_block_height:
                    app_log.info("Outgoing: We have a higher, sending")
                    update_me = 0

                if int(received_block_height) > db_block_height:
                    app_log.info("Outgoing: Node has higher block, receiving")
                    update_me = 1

                if int(received_block_height) == db_block_height:
                    app_log.info("Outgoing: We have the same block height, hash will be verified")
                    update_me = 1

                # print "Update me:"+str(update_me)
                if update_me == 1:
                    conn = sqlite3.connect(ledger_path_conf)
                    conn.text_factory = str
                    c = conn.cursor()
                    execute(c,('SELECT block_hash FROM transactions ORDER BY block_height DESC LIMIT 1'))
                    db_block_hash = c.fetchone()[0]  # get latest block_hash
                    conn.close()

                    app_log.info("Outgoing: block_hash to send: " + str(db_block_hash))
                    send(s, (str(len(db_block_hash))).zfill(10))
                    send(s, str(db_block_hash))

                    # consensus pool 2 (active connection)
                    consensus_blockheight = int(received_block_height)  # str int to remove leading zeros
                    consensus_add(peer_ip, consensus_blockheight)
                    # consensus pool 2 (active connection)

                    # receive their latest hash
                    # confirm you know that hash or continue receiving

                if update_me == 0:  # update them if update_me is 0
                    data = receive(s, 10)  # receive client's last block_hash

                    # send all our followup hashes
                    app_log.info("Outgoing: Will seek the following block: " + str(data))

                    # consensus pool 2 (active connection)
                    consensus_blockheight = int(received_block_height)  # str int to remove leading zeros
                    consensus_add(peer_ip, consensus_blockheight)
                    # consensus pool 2 (active connection)

                    conn = sqlite3.connect(ledger_path_conf)
                    conn.text_factory = str
                    c = conn.cursor()

                    try:
                        execute_param(c, ("SELECT block_height FROM transactions WHERE block_hash = ?;"), (data,))
                        client_block = c.fetchone()[0]

                        app_log.info("Outgoing: Node is at block " + str(
                            client_block))  # now check if we have any newer

                        execute(c,('SELECT block_hash FROM transactions ORDER BY block_height DESC LIMIT 1'))
                        db_block_hash = c.fetchone()[0]  # get latest block_hash
                        if db_block_hash == data:
                            app_log.info("Outgoing: Node has the latest block")
                            send(s, (str(len("nonewblk"))).zfill(10))
                            send(s, "nonewblk")

                        else:
                            execute_param(c,("SELECT block_height, timestamp,address,recipient,amount,signature,public_key,openfield FROM transactions WHERE block_height > ? AND block_height < ?;"), (str(int(client_block)),)+(str(int(client_block + 100)),))  # select incoming transaction + 1, only columns that need not be verified
                            blocks_fetched = c.fetchall()
                            blocks_send = [[l[1:] for l in group] for _, group in groupby(blocks_fetched, key=itemgetter(0))]
                            conn.close()

                            #app_log.info("Outgoing: Selected " + str(blocks_send) + " to send")

                            send(s, (str(len("blocksfnd"))).zfill(10))
                            send(s, "blocksfnd")

                            # send own
                            send(s, (str(len(str(blocks_send)))).zfill(10))
                            send(s, str(blocks_send))
                            # send own
                    except:
                        app_log.info("Outgoing: Block not found")
                        send(s, (str(len("blocknf"))).zfill(10))
                        send(s, "blocknf")

                        send(s, (str(len(data))).zfill(10))
                        send(s, data)

            elif data == "blocknf":
                block_hash_delete = receive(s, 10)
                #print peer_ip
                #print leading_node
                if peer_ip == leading_node:
                    blocknf(block_hash_delete)

                while busy == 1:
                    time.sleep(1)
                send(s, (str(len("sendsync"))).zfill(10))
                send(s, "sendsync")

            elif data == "blocksfnd":
                app_log.info("Outgoing: Node has the block")  # node should start sending txs in this step

                # receive theirs
                segments = receive(s, 10)

                # app_log.info("Incoming: Combined segments: " + segments)
                #print peer_ip
                #print leading_node
                if peer_ip == leading_node:
                    digest_block(segments, peer_ip)
                # receive theirs

                # digest_block(data) goddamn bug
                # digest_block() #temporary

                while busy == 1:
                    time.sleep(1)
                send(s, (str(len("sendsync"))).zfill(10))
                send(s, "sendsync")

                # block_hash validation end

            elif data == "nonewblk":
                # digest_block() #temporary #otherwise passive node will not be able to digest

                # sand and receive mempool
                mempool = sqlite3.connect('mempool.db')
                mempool.text_factory = str
                m = mempool.cursor()
                execute(m,('SELECT * FROM transactions'))
                mempool_txs = m.fetchall()

                #app_log.info("Outgoing: Extracted from the mempool: " + str(mempool_txs))  # improve: sync based on signatures only

                #if len(mempool_txs) > 0: #wont sync mempool until we send something, which is bad
                send(s, (str(len("mempool"))).zfill(10))
                send(s, "mempool")

                # send own
                send(s, (str(len(str(mempool_txs)))).zfill(10))
                send(s, str(mempool_txs))
                # send own

                # receive theirs
                segments = receive(s, 10)
                mempool_merge(segments)
                # receive theirs

                # receive mempool

                app_log.info("Outgoing: We seem to be at the latest block. Paused before recheck")

                time.sleep(int(pause_conf))
                while busy == 1:
                    time.sleep(1)
                send(s, (str(len("sendsync"))).zfill(10))
                send(s, "sendsync")

            else:
                raise ValueError("Unexpected error, received: " + data)

        except Exception as e:
            # remove from active pool
            if this_client in active_pool:
                app_log.info("Will remove " + str(this_client) + " from active pool " + str(active_pool))
                active_pool.remove(this_client)
            # remove from active pool

            # remove from consensus 2
            consensus_remove(peer_ip)
            # remove from consensus 2

            app_log.info("Connection to " + this_client + " terminated due to " + str(e))
            app_log.info("---thread " + str(threading.currentThread()) + " ended---")

            # properly end the connection
            if s:
                s.close()
            # properly end the connection

            if debug_conf == 1:
                raise  # major debug client
            else:
                return


class ThreadedTCPServer(SocketServer.ThreadingMixIn, SocketServer.TCPServer):
    pass


if __name__ == "__main__":
    try:
        # Port 0 means to select an arbitrary unused port
        HOST, PORT = "0.0.0.0", int(port)

        server = ThreadedTCPServer((HOST, PORT), ThreadedTCPRequestHandler)
        ip, port = server.server_address

        purge_old_peers()

        # Start a thread with the server -- that thread will then start one
        # more thread for each request

        server_thread = threading.Thread(target=server.serve_forever)

        # Exit the server thread when the main thread terminates

        server_thread.daemon = True
        server_thread.start()
        app_log.info("Server loop running in thread: " + server_thread.name)

        # start connection manager
        t_manager = threading.Thread(target=manager())
        app_log.info("Starting connection manager")
        t_manager.start()
        # start connection manager

        # server.serve_forever() #added
        server.shutdown()
        server.server_close()

    except Exception, e:
        app_log.info("Node already running?")
        app_log.info(e)
sys.exit()