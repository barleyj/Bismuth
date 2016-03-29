#todo : node needs to broadcast to other nodes so there is no fork in case of malicious send client
import hashlib
import socket
import sys
import re
import ast
import sqlite3
import time
import requests
from Crypto.Hash import SHA256
from Crypto.PublicKey import RSA
from Crypto import Random

port = int(2829)
#"""
#connectivity to self node
prod = 0

if prod == 1:
    r = requests.get(r'http://jsonip.com')
    ip= r.json()['ip']
    print 'Your IP is', ip
    sock_self = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    #sock_self.settimeout(1)
    result = sock_self.connect_ex((ip,port))
    sock_self.close()
    #result = 0 #enable for test
    if result == 0:
        print "Port is open"   
#get local peers into tuples
    peer_file = open("peers.txt", 'r')
    peer_tuples = []
    for line in peer_file:
        extension = re.findall ("'([\d\.]+)', '([\d]+)'",line)
        peer_tuples.extend(extension)
    peer_file.close()
    peer_me = ("('"+str(ip)+"', '"+str(port)+"')")
    if peer_me not in str(peer_tuples): #stringing tuple is a nasty way
        peer_list_file = open("peers.txt", 'a')
        peer_list_file.write((peer_me)+"\n")
        print "Local node saved to peer file"
        peer_list_file.close()
    else:
        print "Self node already saved"
        
#get local peers into tuples    
else:
   print "Port is not open"
#connectivity to self node
#"""
   
# Create a TCP/IP socket
sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
# Bind the socket to the port
server_address = ('0.0.0.0', port)
print 'starting up on %s port %s' % server_address
sock.bind(server_address)

# Listen for incoming connections
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
sock.listen(1)

#verify blockchain
con = None
conn = sqlite3.connect('ledger.db')
c = conn.cursor()
#c.execute("CREATE TABLE IF NOT EXISTS transactions (block_height, address, to_address, amount, signature, public_key)")
c.execute("SELECT Count(*) FROM transactions")
db_rows = c.fetchone()[0]
print "Total steps: "+str(db_rows)

#verify genesis
c.execute("SELECT * FROM transactions ORDER BY block_height ASC LIMIT 1")
genesis = c.fetchone()[2]
print "Genesis: "+genesis
if str(genesis) != "352e5c8ca3751061e63ecb45d4c8dda4deaf773b6cb1e6c18be80072": #change this line to your genesis address if you want to clone
    print "Invalid genesis address"
    sys.exit(1)
#verify genesis

try:
    for row in c.execute('SELECT * FROM transactions ORDER BY block_height'):
        db_block_height = row[0]
        db_address = row[1]
        db_to_address = row[2]
        db_amount = row [3]
        db_signature = row[4]
        db_public_key = RSA.importKey(row[5])
        db_txhash = row[6]
        db_transaction = str(db_address) +":"+ str(db_to_address) +":"+ str(db_amount) 

        #print db_transaction

        db_signature_tuple = ast.literal_eval(db_signature) #converting to tuple
        
        if db_public_key.verify(db_transaction, db_signature_tuple) == True:
            print "Step "+str(db_block_height)+" is valid"
        else:
            print "Step "+str(db_block_height)+" is invalid"
            if db_block_height == str(1):
                print "Your genesis signature is invalid, someone meddled with the database"
                sys.exit(1)
        
except sqlite3.Error, e:                        
    print "Error %s:" % e.args[0]
    sys.exit(1)                        
finally:                        
    if conn:
        conn.close()
#verify blockchain

while True:
    # Wait for a connection
    print 'waiting for a connection'
    
    
    try:
        connection, client_address = sock.accept()
        print 'connection from', client_address
### LOCAL CHECKS FINISHED ###
        
        # Receive and send data
        first_run = 1
        while True:
            try:
                data = connection.recv(11) #one and the only root connection
                print 'Received: '+ data
                
                if data == 'helloserver':
                    with open ("peers.txt", "r") as peer_list:
                        peers=peer_list.read()
                        print peers
                        connection.sendall("peers______")
                        time.sleep(0.1)
                        connection.sendall(peers)
                        time.sleep(0.1)
                        
                    print "Sending sync request"
                    connection.sendall("sync_______")
                    time.sleep(0.1)

                if data == "blockheight":
                    subdata = connection.recv(30) #receive client's last block height
                    received_block_height = subdata
                    print "Received block height: "+(received_block_height)
                    
                    #send own block height
                    conn = sqlite3.connect('ledger.db')
                    c = conn.cursor()                    
                    c.execute('SELECT block_height FROM transactions ORDER BY block_height DESC LIMIT 1')
                    db_block_height = c.fetchone()[0]
                    conn.close()

                    #append zeroes to get static length
                    while len(str(db_block_height)) != 30:
                        db_block_height = "0"+str(db_block_height)
                    connection.sendall(db_block_height)
                    time.sleep(0.1)
                    #send own block height
                    
                    if received_block_height > db_block_height:
                        print "Client has higher block, receiving"
                        update_me = 1
                        #todo
                        
                    if received_block_height <= db_block_height:
                        print "We have a higher or equal block, sending"
                        update_me = 0
                        #todo

                    if received_block_height == db_block_height:
                        print "We have the same block height, hash will be verified"
                        update_me = 0                        

                        if update_me == 0: #update them if update_me is 0
                            data = connection.recv(56) #receive client's last txhash
                            #send all our followup hashes
                            print "Will seek the following block: " + str(data)
                            conn = sqlite3.connect('ledger.db')
                            c = conn.cursor()

                            c.execute("SELECT * FROM transactions WHERE txhash='"+data+"'")
                            try:
                                txhash_client_block = c.fetchone()[0]

                                print "Client is at block "+str(txhash_client_block) #now check if we have any newer

                                c.execute('SELECT txhash FROM transactions ORDER BY block_height DESC LIMIT 1')
                                db_txhash = c.fetchone()[0] #get latest txhash
                                if db_txhash == data:
                                    print "Client has the latest block"
                                    connection.sendall("nonewblocks")
                                    time.sleep(0.1)
                 
                                else:
                                    c.execute("SELECT * FROM transactions WHERE block_height='"+str(int(txhash_client_block) + 1)+"'") #select incoming transaction + 1
                                    txhash_send = c.fetchone()

                                    print "Selected "+str(txhash_send)+" to send"
                                    
                                    conn.close()
                                    connection.sendall("blockfound_")
                                    time.sleep(0.1)
                                    connection.sendall(str(txhash_send))
                                    time.sleep(0.1)
                                
                            except:
                                print "Block not found"
                                connection.sendall("blocknotfoun")
                                time.sleep(0.1)
                            #send all our followup hashes
                        
                            
                #latest local block          
                if data == "transaction":
                    data = connection.recv(1024)
                    data_split = data.split(";")
                    received_transaction = data_split[0]
                    print "Received transaction: "+received_transaction
                    #split message into values
                    try:
                        received_transaction_split = received_transaction.split(":")#todo receive list
                        address = received_transaction_split[0]
                        to_address = received_transaction_split[1]
                        amount = int(received_transaction_split[2])
                    except Exception as e:
                        print "Something wrong with the transaction ("+str(e)+")"
                        break
                    #split message into values
                    received_signature = data_split[1] #needs to be converted
                    received_signature_tuple = ast.literal_eval(received_signature) #converting to tuple
                    print "Received signature: "+received_signature
                    received_public_key_readable = data_split[2]
                    print "Received public key: "+received_public_key_readable
                    received_txhash = data_split[3]
                    print "Received txhash: "+received_txhash

                    #convert received strings
                    received_public_key = RSA.importKey(received_public_key_readable)
                    #convert received strings
                    
                    if received_public_key.verify(received_transaction, received_signature_tuple) == True:
                        print "The signature is valid"
                        #transaction processing

                        conn = sqlite3.connect('ledger.db')
                        c = conn.cursor()
                     
                        #verify balance and blockchain                           
                        print "Verifying balance"
                        print "Address:" +address
                        c.execute("SELECT sum(amount) FROM transactions WHERE to_address = '"+address+"'")
                        credit = c.fetchone()[0]
                        c.execute("SELECT sum(amount) FROM transactions WHERE address = '"+address+"'")
                        debit = c.fetchone()[0]
                        if debit == None:
                            debit = 0
                        if credit == None:
                            credit = 0                                
                        print "Total credit: "+str(credit)                                
                        print "Total debit: "+str(debit)
                        balance = int(credit) - int(debit)
                        print "Your balance: "+str(balance) 

                        if  int(balance) - int(amount) < 0:
                            print "Your balance is too low for this transaction"
                            break
                        else:
                            print "Processing transaction"

                            c.execute('SELECT txhash FROM transactions ORDER BY block_height DESC LIMIT 1')
                            txhash = c.fetchone()[0]
                            c.execute('SELECT block_height FROM transactions ORDER BY block_height DESC LIMIT 1')
                            block_height = c.fetchone()[0]
                            print "Current latest txhash: "+str(txhash)
                            print "Current top block: " +str(block_height)
                            block_height_new = block_height + 1
                            
                            

                            if received_txhash == hashlib.sha224(str(received_transaction) + str(received_signature) +str(txhash)).hexdigest(): #new hash = new tx + new sig + old txhash
                                print "txhash valid"
                                txhash_valid = 1
                                
                                c.execute("INSERT INTO transactions VALUES ('"+str(block_height_new)+"','"+str(address)+"','"+str(to_address)+"','"+str(amount)+"','"+str(received_signature)+"','"+str(received_public_key_readable)+"','"+str(received_txhash)+"')") # Insert a row of data                    
                                #execute transaction                                
                                conn.commit() # Save (commit) the changes
                                #todo: broadcast
                                print "Saved"

                                conn.close()
                                print "Database closed"
                                        
                                #transaction processing                        
                                
                            else:
                                print "txhash invalid"
                                conn.close()
                                break # or something
                                                            
                            #verify balance and blockchain                            
                                #execute transaction
                            

                    else:
                        print "Signature invalid"

                else:
                    print 'no more data from', client_address

            except: #forcibly closed connection
                print "Lost connection"
                raise #for test purposes only
                break
    except:
        raise
            
    finally:
        # Clean up the connection
        if connection:
            connection.close()
