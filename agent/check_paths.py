import os; print('Current Dir:', os.getcwd()); print('Root:', os.listdir('/')); print('App:', os.listdir('/app') if os.path.exists('/app') else 'MISSING')
