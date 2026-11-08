import paramiko

miner_ip = "192.168.1.128"
miner_user = "root"
miner_password = "Beispiel"

def pause_miner():
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(miner_ip, username=miner_user, password=miner_password)
    ssh.exec_command('killall bmminer')  # Stoppt das Mining
    ssh.close()
    print("Miner pausiert.")

if __name__ == "__main__":
    pause_miner()


