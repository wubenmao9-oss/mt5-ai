import ctypes
import os
os.chdir('Z:/')

for dll in ['LoginId.dll', 'LoginX.dll', 'mt4api.dll']:
    try:
        d = ctypes.WinDLL(dll)
        print(dll + ': OK')
    except Exception as e:
        print(dll + ': FAIL - ' + str(e))
