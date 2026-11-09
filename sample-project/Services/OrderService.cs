namespace Sample.Services;
public class OrderService {
    public void Save(int id) { Validator.Check(id); Persist(id); }
    public void Save(string name) { Save(0); }
    public void Refund(int id) { Validator.Check(id); }
    private void Persist(int id) { Console.WriteLine(id); }
}
