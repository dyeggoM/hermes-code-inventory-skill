namespace Sample.Services;
public static class Validator {
    public static void Check(int id) { if (id <= 0) throw new ArgumentOutOfRangeException(nameof(id)); }
}
