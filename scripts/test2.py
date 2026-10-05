
import ctypes
d = ctypes.WinDLL('LoginId.dll')
print('LoginId.dll: OK')
d = ctypes.WinDLL('LoginX.dll')
print('LoginX.dll: OK')
d = ctypes.WinDLL('mt4api.dll')
print('mt4api.dll: OK')
for fn in ['MT4API_Create','MT4API_Init','MT4API_Connect','MT4API_Subscribe','MT4API_OrderSend','MT4API_GetQuote']:
    print('  ' + fn + ': ' + ('OK' if getattr(d, fn, None) else 'MISSING'))
print('All OK!')
