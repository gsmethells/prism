import os
import sys



def outer():
    x = 1


    def inner1():
        y = 2
        return y


    def inner2():
        pass


    class InnerClass:


        def method1(self):
            pass


        def method2(self):
            pass


    return x



def another():
    pass



class TopLevel:


    def __init__(self):
        pass


    def method(self):
        pass
